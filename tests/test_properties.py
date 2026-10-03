"""Property-based tests with Hypothesis (optional: `pip install hypothesis`; the module reports a skip
when it is missing, so the core suite stays dependency-free). Hypothesis generates thousands of inputs,
shrinks failures to a minimal example and remembers them in .hypothesis/ between runs.
"""

from __future__ import annotations

import re

from fakes import run_tests
from ytt.builder import linkify_timestamps, playlist_fingerprint
from ytt.storage import safe_name
from ytt.transcriber import Segment, segments_to_text
from ytt.util import fmt_duration, fmt_ts
from ytt.youtube import Kind, detect_target, normalize_channel_url

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover - exercised only without the dev dependencies
    given = None  # type: ignore[assignment]

if given is None:

    def test_hypothesis_not_installed() -> None:
        print("      SKIP: hypothesis is not installed (pip install hypothesis)")

else:
    FORBIDDEN = set('<>:"/\\|?*') | {chr(c) for c in range(32)}
    ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    video_ids = st.text(alphabet=ID_ALPHABET, min_size=11, max_size=11)
    playlist_ids = st.text(alphabet=ID_ALPHABET, min_size=32, max_size=32).map(lambda s: "PL" + s)

    @given(st.text(max_size=120), st.integers(min_value=3, max_value=80))
    @settings(max_examples=400)
    def test_safe_name_invariants(raw: str, max_len: int) -> None:
        name = safe_name(raw, max_len)
        assert name, "never empty"
        assert len(name) <= max(max_len, 8), "max_len below 8 is treated as 8"
        assert not (set(name) & FORBIDDEN)
        assert name == name.strip(" .") and "  " not in name
        assert safe_name(name, max_len) == name, "idempotent"
        assert name.upper().split(".")[0] not in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }

    segment_lists = st.lists(
        st.tuples(
            st.floats(min_value=0.1, max_value=20),
            st.floats(min_value=0.1, max_value=12),
            st.text(min_size=0, max_size=60),
        ),
        max_size=60,
    )

    @given(segment_lists, st.floats(min_value=0, max_value=60), st.integers(min_value=20, max_value=400), st.booleans())
    @settings(max_examples=300)
    def test_segments_to_text_invariants(raw: list, chunk_sec: float, max_chars: int, timestamps: bool) -> None:
        segments, t = [], 0.0
        for gap, length, text in raw:
            t += gap
            segments.append(Segment(t, t + length, text))
            t += length
        out = segments_to_text(segments, gap_sec=2.0, max_chars=max_chars, timestamps=timestamps, chunk_sec=chunk_sec)
        blocks = out.split("\n\n") if out else []
        body = [re.sub(r"^\[\d{2}:\d{2}:\d{2}\] ", "", b) for b in blocks] if timestamps else blocks
        assert " ".join(body).split() == " ".join(s.text for s in segments).split(), "every word kept, in order"
        if timestamps:
            assert all(re.match(r"^\[\d{2}:\d{2}:\d{2}\] \S", b) for b in blocks), "every block carries a time code"
            starts = [int(b[1:3]) * 3600 + int(b[4:6]) * 60 + int(b[7:9]) for b in blocks]
            assert starts == sorted(starts), "time codes never go backwards"
        longest = max((len(" ".join(s.text.split())) for s in segments), default=0)
        assert all(len(b) <= 2 * max_chars + longest + 12 for b in body), "hard cut honoured"

    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=359_999),
                st.text(min_size=1, max_size=40).filter(lambda s: s.strip() and "\n" not in s),
            ),
            min_size=1,
            max_size=20,
        ),
        video_ids,
    )
    @settings(max_examples=300)
    def test_linkify_roundtrip(blocks: list, video_id: str) -> None:
        text = "\n\n".join(f"[{fmt_ts(sec)}] {body.strip()}" for sec, body in blocks)
        linked = linkify_timestamps(text, video_id)
        assert linked.count("](https://youtu.be/") == len(blocks), "one link per block"
        assert linkify_timestamps(linked, video_id) == linked, "idempotent"
        assert re.sub(r"\]\(https://youtu\.be/[^)]+\)", "]", linked) == text, "stripping the links restores the text"
        for sec, _ in blocks:
            assert f"?t={sec})" in linked

    @given(
        video_ids,
        st.sampled_from(
            [
                "https://youtu.be/{id}",
                "https://www.youtube.com/watch?v={id}",
                "https://www.youtube.com/shorts/{id}",
                "https://m.youtube.com/watch?v={id}&feature=share",
            ]
        ),
    )
    def test_video_urls_detected(video_id: str, template: str) -> None:
        target = detect_target(template.format(id=video_id))
        assert target.kind == Kind.VIDEO and target.video_id == video_id
        assert detect_target(video_id, Kind.VIDEO).video_id == video_id, "bare id with --video"

    @given(playlist_ids)
    def test_playlist_ids_detected(playlist_id: str) -> None:
        for raw in (playlist_id, f"https://www.youtube.com/playlist?list={playlist_id}"):
            target = detect_target(raw)
            assert target.kind == Kind.PLAYLIST and target.playlist_id == playlist_id
        assert detect_target(playlist_id, Kind.CHANNEL).kind == Kind.CHANNEL, "--channel means the owner channel"

    @given(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_.-", min_size=3, max_size=30).filter(
            lambda s: s.strip(".") == s
        )
    )
    def test_normalize_channel_url_is_idempotent(handle: str) -> None:
        once = normalize_channel_url(f"@{handle}")
        assert once == f"https://www.youtube.com/@{handle}"
        assert normalize_channel_url(once) == once and normalize_channel_url(once + "/videos") == once

    @given(st.integers(min_value=0, max_value=999_999))
    def test_time_formatting_roundtrip(seconds: int) -> None:
        h, m, s = (int(x) for x in fmt_ts(seconds).split(":"))
        assert h * 3600 + m * 60 + s == seconds
        parts = [int(x) for x in fmt_duration(seconds).split(":")]
        assert sum(p * 60**i for i, p in enumerate(reversed(parts))) == seconds

    @given(st.lists(video_ids, min_size=2, max_size=8, unique=True), st.randoms())
    def test_fingerprint_sees_order_and_membership(ids: list, rnd) -> None:
        rows = [
            {"id": v, "position": i, "status": "done", "transcribed_at": "t", "title": v} for i, v in enumerate(ids)
        ]
        pl = {"id": "PLx", "title": "x"}
        base = playlist_fingerprint(pl, rows)
        shuffled = rows[:]
        rnd.shuffle(shuffled)
        if [r["id"] for r in shuffled] != ids:
            assert playlist_fingerprint(pl, shuffled) != base
        assert playlist_fingerprint(pl, rows[:-1]) != base
        assert playlist_fingerprint(pl, rows) == base


if __name__ == "__main__":
    run_tests(globals())
