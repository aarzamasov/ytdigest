"""Unit tests for the small building blocks: config loading, util helpers, DB status transitions and
membership queries, the Downloader (real subprocesses), file naming and paragraph assembly (including
seeded randomized property checks), and the Whisper adapters driven by stand-in libraries.
"""

from __future__ import annotations

import random
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

from fakes import expect_raises, run_tests
from ytt.config import Config, DownloadConfig, TranscribeConfig
from ytt.db import Database, Status
from ytt.downloader import Downloader, DownloadError
from ytt.storage import Layout, safe_name
from ytt.transcriber import FasterWhisperTranscriber, MlxWhisperTranscriber, Segment, get_transcriber, segments_to_text
from ytt.util import fmt_duration, fmt_ts, setup_logging, sha256_text, utcnow_iso
from ytt.youtube import VideoInfo

UC = "UC" + "u" * 22


class TempDir:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="ytt-unit-"))
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# ------------------------------------------------------------------------------------- config
def test_config_loading() -> None:
    with TempDir() as tmp:
        toml = tmp / "cfg" / "config.toml"
        toml.parent.mkdir()
        toml.write_text(
            '[paths]\ndata_dir = "store"\n[transcribe]\nlanguage = ""\nmodel = "small"\nmystery = 1\n'
            "[download]\nmax_attempts = 7\n[nope]\nx = 1\n",
            encoding="utf-8",
        )
        cfg = Config.load(toml)
        assert cfg.paths.data_dir == str((toml.parent / "store").resolve()), (
            "relative data_dir is anchored to the config file"
        )
        assert cfg.transcribe.language is None and cfg.transcribe.model == "small"
        assert (
            cfg.transcribe.timestamps is True
            and cfg.transcribe.chunk_sec == 10.0
            and cfg.output.timestamp_links is True
        )
        assert cfg.download.max_attempts == 7 and cfg.download.timeout_sec == 1800, "unspecified keys keep defaults"
        assert not hasattr(cfg.transcribe, "mystery"), "unknown keys are ignored"

        sections = tmp / "sections.toml"
        sections.write_text(
            '[youtube]\nskip_live = false\nchannel_tabs = ["videos", "shorts"]\n[output]\ntimestamp_links = false\nmax_name_len = 30\n'
            '[download]\nkeep_audio = false\n[paths]\nlog_file = "run.log"\n[transcribe]\nmistyped = true\n',
            encoding="utf-8",
        )
        import logging

        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        config_log = logging.getLogger("ytt.config")
        config_log.addHandler(handler)
        logging.disable(logging.NOTSET)  # fakes.py silences logging; the warning is the behaviour under test
        try:
            cfg = Config.load(sections)
        finally:
            logging.disable(logging.CRITICAL)
            config_log.removeHandler(handler)
        assert cfg.youtube.skip_live is False and cfg.youtube.channel_tabs == ["videos", "shorts"]
        assert cfg.output.timestamp_links is False and cfg.output.max_name_len == 30
        assert cfg.download.keep_audio is False and cfg.paths.log_file == "run.log"
        warnings = [r for r in records if r.levelno == logging.WARNING]
        assert warnings and "transcribe" in warnings[0].getMessage() and "mistyped" in warnings[0].getMessage(), (
            "typos in config.toml are reported"
        )

        absolute = tmp / "abs.toml"
        absolute.write_text(
            f'[paths]\ndata_dir = "{(tmp / "elsewhere").as_posix()}"\n[transcribe]\nlanguage = "auto"\n',
            encoding="utf-8",
        )
        cfg = Config.load(absolute)
        assert Path(cfg.paths.data_dir) == tmp / "elsewhere" and cfg.transcribe.language is None

    defaults = Config.load(None)  # repo config.toml (mirrors the defaults) or pure defaults
    assert Path(defaults.paths.data_dir).is_absolute() and defaults.transcribe.language == "ru"
    assert Config().download.command.startswith("yt-dlp") and "{output_template}" in Config().download.command


# --------------------------------------------------------------------------------------- util
def test_util_helpers() -> None:
    assert fmt_duration(None) == "?" and fmt_duration(59) == "0:59" and fmt_duration(3723) == "1:02:03"
    assert fmt_duration(3600) == "1:00:00" and fmt_duration(245.9) == "4:05"
    assert fmt_ts(0) == "00:00:00" and fmt_ts(3723.4) == "01:02:03"
    stamp = utcnow_iso()
    assert stamp.endswith("+00:00") and "." not in stamp and len(stamp) == 25
    assert sha256_text("a") == sha256_text("a") != sha256_text("b") and len(sha256_text("")) == 64
    with TempDir() as tmp:
        setup_logging(tmp / "logs" / "ytt.log", verbose=True)
        import logging

        logging.getLogger("ytt.test").debug("hello file")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert (tmp / "logs" / "ytt.log").exists()
        setup_logging(None)  # console only, no file


# ----------------------------------------------------------------------------------------- db
def info(vid: str, title: str = "T", available: bool = True, live: str | None = None) -> VideoInfo:
    return VideoInfo(vid, title, f"https://www.youtube.com/watch?v={vid}", 100, "2025-01-01", available, live)


def test_db_video_status_transitions() -> None:
    with TempDir() as tmp:
        db = Database(tmp / "t.sqlite3")
        try:
            db.upsert_channel(UC, "https://www.youtube.com/@u", "U", "U", "t0")
            assert db.get_channel(UC)["last_synced"] is None, "NULL until the first full sync"
            v = "a" * 11
            db.upsert_video(UC, info(v, "Title"), Status.PENDING, None, "t1")
            assert db.get_video(v)["status"] == "pending"

            db.upsert_video(
                UC, info(v, "[Private video]", available=False), Status.SKIPPED, "unavailable (private)", "t2"
            )
            row = db.get_video(v)
            assert row["status"] == "skipped" and row["error"].startswith("unavailable")
            assert row["title"] == "Title", "a placeholder title does not overwrite the real one"

            db.upsert_video(UC, info(v, "Title v2"), Status.PENDING, None, "t3")
            row = db.get_video(v)
            assert row["status"] == "pending" and row["error"] is None and row["title"] == "Title v2"

            db.bump_attempts(v)
            db.set_audio(v, "/a.mp3")
            assert db.get_video(v)["status"] == "downloaded"
            db.mark_done(v, "/t.txt", "PLx", "t4")
            db.upsert_video(UC, info(v, available=False), Status.SKIPPED, "unavailable", "t5")
            assert db.get_video(v)["status"] == "done", "sync never touches done videos"
            db.set_audio(v, None)
            assert db.get_video(v)["status"] == "done" and db.get_video(v)["audio_path"] is None

            w = "b" * 11
            db.upsert_video(UC, info(w), Status.PENDING, None, "t1")
            db.mark_error(w, "boom")
            db.upsert_video(UC, info(w, live="is_live"), Status.SKIPPED, "live stream (is_live)", "t2")
            assert db.get_video(w)["status"] == "skipped", "error -> skipped when the video becomes unprocessable"
            db.upsert_video(UC, info(w), Status.PENDING, None, "t3")
            db.mark_error(w, "boom")
            db.bump_attempts(w)
            assert db.reset_errors(UC, ["c" * 11]) == 0 and db.reset_errors(UC, [w]) == 1
            row = db.get_video(w)
            assert row["status"] == "pending" and row["attempts"] == 0 and row["error"] is None
            db.reset_video(v)
            assert db.get_video(v)["transcript_path"] is None and db.get_video(v)["status"] == "pending"
        finally:
            db.close()


def test_db_playlists_and_membership() -> None:
    with TempDir() as tmp:
        db = Database(tmp / "t.sqlite3")
        try:
            db.upsert_channel(UC, "https://www.youtube.com/@u", "U", "U", "t0")
            assert db.next_playlist_position(UC) == 0
            db.upsert_playlist(UC, "PLa", "A", "ua", 0, "A", False, "t0")
            db.upsert_playlist(UC, "PLb", "B", "ub", 1, "B", False, "t0", manual=True)
            db.upsert_playlist(UC, "__unsorted__" + UC, "Без", "uu", 1_000_000, "Без", True, "t0")
            assert db.next_playlist_position(UC) == 2, "the virtual playlist does not count"
            assert [p["id"] for p in db.active_playlists(UC)] == ["PLa", "PLb", "__unsorted__" + UC]

            db.upsert_playlist(UC, "PLb", "B renamed", "ub", 5, "ignored-dir", False, "t1")
            b = db.get_playlist("PLb")
            assert (b["title"], b["position"], b["dir_name"], b["manual"]) == ("B renamed", 5, "B", 1), (
                "dir_name and manual survive updates"
            )

            assert db.deactivate_missing_playlists(UC, ["PLa"]) == 0, (
                "manual and virtual playlists are never deactivated"
            )
            db.upsert_playlist(UC, "PLc", "C", "uc", 2, "C", False, "t1")
            assert db.deactivate_missing_playlists(UC, ["PLa"]) == 1 and db.get_playlist("PLc")["active"] == 0
            db.upsert_playlist(UC, "PLc", "C", "uc", 2, "C", False, "t2")
            assert db.get_playlist("PLc")["active"] == 1, "reappearing playlists are reactivated"

            for n in ("x", "y", "z"):
                db.upsert_video(UC, info(n * 11), Status.PENDING, None, "t0")
            db.replace_membership("PLa", [("x" * 11, 0), ("y" * 11, 1)])
            db.add_membership("PLb", "y" * 11)
            db.add_membership("PLb", "z" * 11)
            db.add_membership("PLb", "z" * 11)  # idempotent
            assert [(r["id"], r["position"]) for r in db.playlist_videos("PLb")] == [("y" * 11, 0), ("z" * 11, 1)]
            assert db.video_playlist_ids("y" * 11) == ["PLa", "PLb"]
            assert db.video_ids_in_real_playlists(UC) == {"x" * 11, "y" * 11, "z" * 11}
            assert db.membership_pairs(UC) == {
                ("PLa", "x" * 11),
                ("PLa", "y" * 11),
                ("PLb", "y" * 11),
                ("PLb", "z" * 11),
            }
            assert db.remove_membership("PLa", ["y" * 11, "q" * 11]) == 1 and db.remove_membership("PLa", []) == 0
            assert db.video_playlist_ids("y" * 11) == ["PLb"]
            assert db.entity_counts(UC) == (3, 3)
            stats = {s["id"]: s["total"] for s in db.playlist_stats(UC)}
            assert stats["PLa"] == 1 and stats["PLb"] == 2
        finally:
            db.close()


# -------------------------------------------------------------------------------- downloader
def test_downloader_subprocess_paths() -> None:
    py = Path(sys.executable).as_posix()
    with TempDir() as tmp:
        ok = Downloader(
            DownloadConfig(
                command=(
                    f"\"{py}\" -c \"import sys,pathlib;pathlib.Path(sys.argv[1].replace('%(ext)s','mp3')).write_bytes(b'x')\" "
                    '"{output_template}" "{url}" "{unknown}"'
                )
            )
        )
        ok.check()
        path = ok.download("https://www.youtube.com/watch?v=abcdefghijk", "abcdefghijk", tmp)
        assert path == tmp / "abcdefghijk.mp3"
        assert ok.download("u", "abcdefghijk", tmp) == path, "existing audio is reused, the command is not run again"

        # Every placeholder is substituted; the audio directory is created even when nested.
        echo = Downloader(
            DownloadConfig(
                command=(
                    f"\"{py}\" -c \"import sys,pathlib;pathlib.Path(sys.argv[1].replace('%(ext)s','mp3')).write_text(chr(10).join(sys.argv[2:]))\" "
                    '"{output_template}" "{url}" "{video_id}" "{output_dir}" "{unknown}"'
                )
            )
        )
        nested = tmp / "nested" / "deeper"
        written = echo.download("https://youtu.be/zyxwvutsrqp", "zyxwvutsrqp", nested).read_text().splitlines()
        assert written == ["https://youtu.be/zyxwvutsrqp", "zyxwvutsrqp", nested.as_posix(), "{unknown}"]
        (tmp / "zzzzzzzzzzz.m4a").write_bytes(b"audio")
        assert ok.find_existing(tmp, "zzzzzzzzzzz").suffix == ".m4a", "any known audio extension counts"

        failing = Downloader(
            DownloadConfig(command=f'"{py}" -c "import sys; print(\'bad\', file=sys.stderr); sys.exit(3)" "{{url}}"')
        )
        exc = expect_raises(DownloadError, failing.download, "u", "qqqqqqqqqqq", tmp, contains="exit code 3")
        assert "bad" in str(exc), "the tail of stderr is part of the message"

        silent = Downloader(DownloadConfig(command=f'"{py}" -c "pass" "{{url}}"'))
        expect_raises(DownloadError, silent.download, "u", "wwwwwwwwwww", tmp, contains="no audio file was produced")

        slow = Downloader(DownloadConfig(command=f'"{py}" -c "import time; time.sleep(5)" "{{url}}"', timeout_sec=1))
        expect_raises(DownloadError, slow.download, "u", "sssssssssss", tmp, contains="timed out")

        missing = Downloader(DownloadConfig(command="definitely-not-a-real-binary-xyz {url}"))
        expect_raises(DownloadError, missing.check, contains="not found in PATH")
        exc = expect_raises(DownloadError, missing.download, "u", "mmmmmmmmmmm", tmp)
        assert "definitely-not-a-real-binary-xyz" in str(exc), "the missing executable is named"
        quiet_fail = Downloader(DownloadConfig(command=f'"{py}" -c "import sys; sys.exit(2)" "{{url}}"'))
        expect_raises(DownloadError, quiet_fail.download, "u", "qqqqqqqqqq2", tmp, contains="no output")


# ---------------------------------------------------------------------------- names & layout
def test_safe_name_and_layout() -> None:
    assert safe_name("Доктор: Тест/Канал?") == "Доктор Тест Канал"
    assert safe_name("a" * 100, max_len=10) == "a" * 10
    assert safe_name("word " * 20, max_len=12) == "word word", "cut at a word boundary"
    assert (
        safe_name("CON") == "CON_"
        and safe_name("lpt1") == "lpt1_"
        and safe_name("   ") == "untitled"
        and safe_name("x..") == "x"
    )
    assert safe_name("tab\there\nnew") == "tab here new"
    assert safe_name("a" * 10, 10) == "a" * 10, "exactly max_len stays intact"
    assert safe_name("abcde fghijklmno", 10) == "abcde fghi", "a space at max_len//2 is too early for a word cut"
    assert safe_name("abcdef ghijklmno", 10) == "abcdef", "a space past max_len//2 is used as the cut"
    assert safe_name("RELAX NOWHERE", 8) == "RELAX", "only spaces and dots are stripped after the cut"
    assert safe_name("con.1") == "con_.1" and safe_name("CON [abcdefghijk]") == "CON [abcdefghijk]", (
        "device names, also with an extension"
    )
    assert safe_name("", 3) == "untitled" and len(safe_name("word " * 5, 3)) <= 8, "max_len below 8 is clamped"

    forbidden = set('<>:"/\\|?*') | {chr(c) for c in range(32)}
    rng = random.Random(2026)
    alphabet = 'abcXYZ Йцу  ..<>:"/\\|?*\t\n\x00éß日本' + "".join(chr(c) for c in range(0x400, 0x420))
    for _ in range(400):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 90)))
        max_len = rng.randint(3, 70)
        name = safe_name(raw, max_len)
        assert name and len(name) <= max(max_len, 8) and not (set(name) & forbidden), repr(raw)
        assert name == name.strip(" .") and "  " not in name, repr(raw)
        assert safe_name(name, max_len) == name, f"not idempotent for {raw!r}"

    cfg = Config()
    cfg.paths.data_dir = "/data"
    layout = Layout(cfg, "Канал")
    path = layout.transcript_path("Плейлист", 7, "Название: с/запрещёнными?", "abcdefghijk")
    assert path.as_posix() == "/data/Канал/transcripts/Плейлист/007 - Название с запрещёнными [abcdefghijk].txt"
    assert layout.playlist_md_path("Плейлист").as_posix() == "/data/Канал/output/Плейлист.md"
    assert layout.channel_md_path().name == "_ALL.md" and layout.audio_dir.as_posix() == "/data/Канал/audio"


def test_segments_to_text_rules_and_properties() -> None:
    segments = [Segment(0, 2, "Первое предложение."), Segment(2.5, 4, "Второе."), Segment(7, 9, "После паузы.")]
    assert segments_to_text(segments, gap_sec=2.0) == "Первое предложение. Второе.\n\nПосле паузы."
    assert (
        segments_to_text(segments, gap_sec=2.0, timestamps=True)
        == "[00:00:00] Первое предложение. Второе.\n\n[00:00:07] После паузы."
    )
    assert segments_to_text([Segment(0, 1, "  "), Segment(1, 2, " x  y ")]) == "x y", (
        "whitespace collapsed, blanks dropped"
    )
    assert segments_to_text([]) == ""
    # Exact boundaries (mutation testing showed these were unguarded).
    pause = [Segment(0, 1, "А."), Segment(3.0, 4, "Б.")]
    assert segments_to_text(pause, gap_sec=2.0) == "А.\n\nБ.", "a pause of exactly gap_sec starts a new block"
    assert segments_to_text([Segment(0, 1, "А."), Segment(2.9, 4, "Б.")], gap_sec=2.0) == "А. Б."
    sized = [Segment(0, 1, "abcd."), Segment(1, 2, "efgh."), Segment(2, 3, "ijkl.")]  # buffer length 6 after the first
    assert segments_to_text(sized, gap_sec=99, max_chars=6) == "abcd.\n\nefgh.\n\nijkl.", "soft limit reached exactly"
    assert segments_to_text(sized, gap_sec=99, max_chars=7) == "abcd. efgh.\n\nijkl.", (
        "one char below the limit: no split yet"
    )
    ended = [Segment(0, 1, "abcdefg"), Segment(1, 2, "hij."), Segment(2, 3, "klm")]
    assert segments_to_text(ended, gap_sec=99, max_chars=8) == "abcdefg hij.\n\nklm", (
        "soft split waits for a sentence end"
    )
    not_ended = [Segment(0, 1, "abcdefg"), Segment(1, 2, "hij"), Segment(2, 3, "klm.")]
    assert segments_to_text(not_ended, gap_sec=99, max_chars=8) == "abcdefg hij klm.", "no sentence end: keep going"
    hard = [Segment(0, 1, "abcde"), Segment(1, 2, "fghij"), Segment(2, 3, "klm")]
    assert segments_to_text(hard, gap_sec=99, max_chars=6) == "abcde fghij\n\nklm", "hard cut at exactly 2x"
    assert segments_to_text([Segment(0, 1, "А."), Segment(10.0, 11, "Б.")], gap_sec=99, chunk_sec=10) == "А.\n\nБ.", (
        "span of exactly chunk_sec"
    )
    assert segments_to_text([Segment(0, 1, "А."), Segment(9.9, 11, "Б.")], gap_sec=99, chunk_sec=10) == "А. Б."
    assert segments_to_text([Segment(0, 0.3, "А."), Segment(0.6, 1, "Б.")], gap_sec=99, chunk_sec=0.5) == "А.\n\nБ.", (
        "sub-second chunk_sec counts"
    )
    # Time-based blocks: close at the first segment boundary once chunk_sec is spanned; sentences stay whole.
    speech = [
        Segment(0, 3.5, "Раз."),
        Segment(3.7, 8.9, "Два."),
        Segment(9.1, 14.0, "Три."),
        Segment(14.2, 19.5, "Четыре."),
        Segment(19.6, 24.0, "Пять."),
        Segment(27.0, 30.0, "Шесть."),
    ]
    assert segments_to_text(speech, gap_sec=2.0, chunk_sec=10, timestamps=True) == (
        "[00:00:00] Раз. Два. Три.\n\n[00:00:14] Четыре. Пять.\n\n[00:00:27] Шесть."
    )
    assert (
        segments_to_text(speech, gap_sec=2.0, chunk_sec=0, timestamps=True)
        == "[00:00:00] Раз. Два. Три. Четыре. Пять.\n\n[00:00:27] Шесть."
    )
    assert segments_to_text(speech, gap_sec=100, chunk_sec=5, max_chars=900).count("\n\n") == 4, "a block per ~5 s"
    # Soft limit cuts at a sentence end; a paragraph never grows past roughly twice the limit.
    long = [Segment(i, i + 0.5, f"Предложение {i}.") for i in range(40)]
    paragraphs = segments_to_text(long, gap_sec=5, max_chars=100).split("\n\n")
    assert len(paragraphs) > 1 and all(p.endswith(".") for p in paragraphs)

    rng = random.Random(7)
    for _ in range(150):
        segs, t = [], 0.0
        for _ in range(rng.randint(0, 60)):
            t += rng.choice([0.1, 0.5, 1.0, 3.0, 10.0])
            words = " ".join(
                rng.choice(["слово", "фраза.", "и", "конец!", "ещё", "так"]) for _ in range(rng.randint(1, 8))
            )
            segs.append(Segment(t, t + 1, words))
            t += 1
        max_chars = rng.randint(20, 300)
        text = segments_to_text(segs, gap_sec=2.0, max_chars=max_chars, chunk_sec=rng.choice([0, 5, 10, 30]))
        assert text.split() == " ".join(s.text for s in segs).split(), "every word kept, order kept"
        longest_segment = max((len(" ".join(s.text.split())) for s in segs), default=0)
        assert all(len(p) <= 2 * max_chars + longest_segment + 1 for p in text.split("\n\n")), "hard cut honoured"


# ------------------------------------------------------------------------------- transcriber
def test_whisper_adapters_with_stand_in_libraries() -> None:
    cfg = TranscribeConfig(model="large-v3-turbo", language="ru", device="cpu", compute_type="int8")

    class WhisperModel:  # minimal shape of faster_whisper.WhisperModel
        def __init__(self, model, device, compute_type):
            self.args = (model, device, compute_type)

        def transcribe(self, path, **kwargs):
            assert kwargs == {"language": "ru", "beam_size": 5, "vad_filter": True, "condition_on_previous_text": False}
            segments = (types.SimpleNamespace(start=s, end=s + 2.0, text=f" seg {s}") for s in (0.0, 700.0, 1300.0))
            return segments, types.SimpleNamespace(duration=1400.0, language="ru", language_probability=0.98)

    fake_fw = types.ModuleType("faster_whisper")
    fake_fw.WhisperModel = WhisperModel
    with mock.patch.dict(sys.modules, {"faster_whisper": fake_fw}):
        t = get_transcriber(cfg)
        assert isinstance(t, FasterWhisperTranscriber) and t.model.args == ("large-v3-turbo", "cpu", "int8")
        out = t.transcribe(Path("a.mp3"))
        assert [(s.start, s.end, s.text) for s in out] == [
            (0.0, 2.0, " seg 0.0"),
            (700.0, 702.0, " seg 700.0"),
            (1300.0, 1302.0, " seg 1300.0"),
        ]

    fake_mlx = types.ModuleType("mlx_whisper")
    seen = {}

    def transcribe(path, **kwargs):
        seen.update(kwargs, path=path)
        return {"segments": [{"start": 0, "end": 1.5, "text": " hi"}], "text": " hi"}

    fake_mlx.transcribe = transcribe
    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_mlx}):
        t = get_transcriber(TranscribeConfig(backend="mlx-whisper", model="large-v3-turbo"))
        assert isinstance(t, MlxWhisperTranscriber) and t.repo == "mlx-community/whisper-large-v3-turbo"
        assert [(s.start, s.end, s.text) for s in t.transcribe(Path("b.mp3"))] == [(0.0, 1.5, " hi")]
        assert seen["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo" and seen["language"] == "ru"
        assert MlxWhisperTranscriber(TranscribeConfig(model="someone/custom-repo")).repo == "someone/custom-repo"
        assert MlxWhisperTranscriber(TranscribeConfig(model="odd")).repo == "mlx-community/whisper-odd"

    with mock.patch.dict(sys.modules, {"faster_whisper": None, "mlx_whisper": None}):  # not installed
        expect_raises(RuntimeError, FasterWhisperTranscriber, cfg, contains="pip install faster-whisper")
        expect_raises(RuntimeError, MlxWhisperTranscriber, cfg, contains="pip install mlx-whisper")
    expect_raises(
        ValueError, get_transcriber, TranscribeConfig(backend="whisperx"), contains="Unknown transcribe.backend"
    )


if __name__ == "__main__":
    run_tests(globals())
