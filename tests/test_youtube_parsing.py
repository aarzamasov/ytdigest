"""ytt/youtube.py against yt-dlp-shaped metadata — the part of the code base that cannot be exercised
without the network. Two layers:

1. Hand-written dicts in the shape yt-dlp returns (flat playlist entries, channel tabs, one video page),
   plus resolvers driven through a stubbed `_extract`.
2. Recorded fixtures in tests/fixtures/*.json (made with `python tests/record_fixtures.py <channel>`):
   whatever is there is parsed and sanity-checked, so a yt-dlp field rename shows up here first.
"""

from __future__ import annotations

import json
from pathlib import Path

from fakes import expect_raises, run_tests
from ytt.config import YouTubeConfig
from ytt.youtube import (
    YouTube,
    YouTubeError,
    _channel_from_info,
    _iter_entries,
    _playlist_from_entry,
    _video_from_entry,
    _videos_from_info,
    normalize_channel_url,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
UC = "UC" + "a" * 22


def flat_video(vid: str, title: str = "Video", **extra) -> dict:
    """A flat entry as yt-dlp emits it for playlists and channel tabs (extract_flat=True)."""
    return {
        "_type": "url",
        "ie_key": "Youtube",
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": title,
        "duration": 613,
        "channel_id": UC,
        "channel": "Doc",
        "uploader": "Doc",
        "uploader_id": "@doc",
        "uploader_url": "https://www.youtube.com/@doc",
        "view_count": 10,
        "live_status": None,
        "availability": None,
        "timestamp": None,
        "release_timestamp": None,
        **extra,
    }


def flat_playlist(pid: str, title: str = "Playlist") -> dict:
    return {
        "_type": "url",
        "ie_key": "YoutubeTab",
        "id": pid,
        "url": f"https://www.youtube.com/playlist?list={pid}",
        "title": title,
        "playlist_count": 3,
    }


def tab_info(entries: list, tab: str = "Videos") -> dict:
    return {
        "_type": "playlist",
        "id": UC,
        "title": f"Doc - {tab}",
        "channel": "Doc",
        "channel_id": UC,
        "uploader": "Doc",
        "uploader_id": "@doc",
        "uploader_url": "https://www.youtube.com/@doc",
        "channel_url": f"https://www.youtube.com/channel/{UC}",
        "entries": entries,
    }


def playlist_info(pid: str, entries: list, owner: bool = True) -> dict:
    info = {"_type": "playlist", "id": pid, "title": "Сердце", "entries": entries, "modified_date": "20250101"}
    if owner:
        info.update(channel="Doc", channel_id=UC, uploader="Doc", uploader_url="https://www.youtube.com/@doc")
    return info


def video_page(vid: str, **extra) -> dict:
    """What a full (non-flat) extraction of one watch page looks like, formats stripped."""
    return {
        "id": vid,
        "title": "Full video",
        "duration": 3723,
        "upload_date": "20240115",
        "timestamp": 1705300000,
        "channel_id": UC,
        "channel": "Doc",
        "channel_url": f"https://www.youtube.com/channel/{UC}",
        "uploader": "Doc",
        "uploader_id": "@doc",
        "uploader_url": "https://www.youtube.com/@doc",
        "live_status": "not_live",
        "availability": "public",
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        **extra,
    }


# ------------------------------------------------------------------------------ pure parsers
def test_video_from_entry() -> None:
    v = _video_from_entry(flat_video("dQw4w9WgXcQ", "Гипертония", timestamp=1735689600))
    assert (v.id, v.title, v.duration, v.available, v.is_live) == ("dQw4w9WgXcQ", "Гипертония", 613, True, False)
    assert v.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ" and v.upload_date == "2025-01-01"

    private = _video_from_entry(flat_video("dQw4w9WgXcQ", "[Private video]", duration=None))
    assert private.available is False and private.duration is None
    assert _video_from_entry(flat_video("dQw4w9WgXcQ", "[Deleted video]")).available is False
    assert _video_from_entry(flat_video("dQw4w9WgXcQ", "x", availability="private")).available is False

    live = _video_from_entry(flat_video("dQw4w9WgXcQ", "Эфир", live_status="is_live", duration=None))
    assert live.is_live and live.live_status == "is_live"
    assert _video_from_entry(flat_video("dQw4w9WgXcQ", "Запись", live_status="was_live")).is_live is False
    upcoming = _video_from_entry(
        flat_video("dQw4w9WgXcQ", "Скоро", live_status="is_upcoming", release_timestamp=1800000000)
    )
    assert upcoming.is_live and upcoming.upload_date == "2027-01-15", "release_timestamp is the fallback date"
    assert _video_from_entry(flat_video("dQw4w9WgXcQ", "x", upload_date="20250101")).upload_date == "2025-01-01"
    assert _video_from_entry(flat_video("dQw4w9WgXcQ", "x", upload_date="2025-01-01")).upload_date is None, (
        "only YYYYMMDD is understood"
    )

    page = _video_from_entry(video_page("dQw4w9WgXcQ"))
    assert page.upload_date == "2024-01-15" and page.duration == 3723 and page.available

    assert _video_from_entry(flat_video("", "no id")) is None
    assert _video_from_entry(flat_playlist("PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")) is None, "a playlist is not a video"
    assert _video_from_entry({"id": "dQw4w9WgXcQ", "title": None}).title == "dQw4w9WgXcQ", (
        "missing title falls back to the id"
    )


def test_playlist_from_entry_and_iteration() -> None:
    pl = _playlist_from_entry(flat_playlist("PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "Питание"))
    assert (pl.id, pl.title, pl.url) == (
        "PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "Питание",
        "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    )
    assert _playlist_from_entry(flat_video("dQw4w9WgXcQ")) is None, "a video on the Playlists tab is ignored"
    # Some entries only carry the id in the URL.
    assert (
        _playlist_from_entry({"url": "https://www.youtube.com/playlist?list=PLyyyyyyyyyyyyyyyyyyyy", "title": "T"}).id
        == "PLyyyyyyyyyyyyyyyyyyyy"
    )

    # Channel home / featured tabs nest shelves; None entries appear for extraction errors.
    nested = tab_info(
        [
            {"_type": "playlist", "title": "Shelf", "entries": [flat_video("a" * 11), None, flat_video("b" * 11)]},
            flat_video("c" * 11),
            flat_video("a" * 11),  # duplicate across shelves
        ]
    )
    assert [e["id"] for e in _iter_entries(nested)] == ["a" * 11, "b" * 11, "c" * 11, "a" * 11]
    assert [v.id for v in _videos_from_info(nested)] == ["a" * 11, "b" * 11, "c" * 11], "deduplicated, order kept"
    assert _videos_from_info({"entries": None}) == []


def test_channel_from_info() -> None:
    ch = _channel_from_info(tab_info([]))
    assert (ch.id, ch.name, ch.url) == (UC, "Doc", "https://www.youtube.com/@doc")
    # Playlist pages name the owner the same way; a mix has no owner.
    assert _channel_from_info(playlist_info("PLx", [])).id == UC
    assert _channel_from_info(playlist_info("RDx", [], owner=False)) is None
    # Tab pages may lack channel fields but carry the UC id as their own id (allow_own_id).
    bare = {"id": UC, "title": "Doc - Playlists", "entries": []}
    assert _channel_from_info(bare) is None
    own = _channel_from_info(bare, fallback_url="https://www.youtube.com/@doc", allow_own_id=True)
    assert own.id == UC and own.name == "Doc" and own.url == "https://www.youtube.com/@doc"
    assert _channel_from_info({"channel_id": "not-a-channel"}) is None
    assert _channel_from_info({"channel_id": UC}).url == f"https://www.youtube.com/channel/{UC}"
    only_channel = _channel_from_info(
        {"channel_id": UC, "channel": "Doc", "channel_url": f"https://www.youtube.com/channel/{UC}"}
    )
    assert only_channel.name == "Doc" and only_channel.url == f"https://www.youtube.com/channel/{UC}", (
        "channel fields without uploader"
    )
    assert _channel_from_info({"channel_id": UC, "uploader": "Up"}).name == "Up"


def test_normalize_channel_url() -> None:
    expected = "https://www.youtube.com/@doc"
    for raw in (
        "@doc",
        "doc",
        "https://www.youtube.com/@doc/videos",
        "http://m.youtube.com/@doc/playlists?view=1",
        "youtube.com/@doc/",
        "https://youtube.com/@doc#x",
        "https://www.youtube.com/@doc/streams/",
    ):
        assert normalize_channel_url(raw) == expected, raw
    assert normalize_channel_url(UC) == f"https://www.youtube.com/channel/{UC}"
    assert normalize_channel_url("HTTPS://WWW.YouTube.com/@doc/videos") == expected, (
        "scheme and host are case-insensitive"
    )
    assert normalize_channel_url("http://youtube.com/@doc") == expected
    assert normalize_channel_url("https://www.youtube.com/c/DocName/videos") == "https://www.youtube.com/c/DocName"
    assert normalize_channel_url("https://www.youtube.com/user/DocName") == "https://www.youtube.com/user/DocName"


# ------------------------------------------------------------------------- resolvers (stubbed)
class StubYouTube(YouTube):
    """Real YouTube class with `_extract` replaced by a URL -> dict table (a dict value raises)."""

    def __init__(self, table: dict, cfg: YouTubeConfig | None = None) -> None:
        super().__init__(cfg or YouTubeConfig())
        self.table = table
        self.requests: list[tuple[str, bool]] = []

    def _extract(self, url: str, flat: bool = True) -> dict:
        self.requests.append((url, flat))
        result = self.table.get(url)
        if result is None:
            raise YouTubeError(f"stub: nothing for {url}")
        return result


def test_resolve_channel_falls_through_tabs() -> None:
    yt = StubYouTube({"https://www.youtube.com/@doc/playlists": tab_info([], "Playlists")})
    ch = yt.resolve_channel("@doc/videos")
    assert ch.id == UC and ch.url == "https://www.youtube.com/@doc"
    assert [u for u, _ in yt.requests] == [
        "https://www.youtube.com/@doc/videos",
        "https://www.youtube.com/@doc/playlists",
    ]

    # Nothing carries a channel id -> clear error mentioning what the user typed.
    none = StubYouTube({"https://www.youtube.com/@doc/videos": {"id": "not-a-channel", "entries": []}})
    expect_raises(YouTubeError, none.resolve_channel, "@doc", contains="@doc")


def test_list_playlists_and_videos() -> None:
    pl1, pl2 = "PL" + "1" * 32, "PL" + "2" * 32
    table = {
        "https://www.youtube.com/@doc/playlists": tab_info(
            [flat_playlist(pl1, "A"), flat_video("v" * 11), flat_playlist(pl2, "B"), flat_playlist(pl1, "A again")],
            "Playlists",
        ),
        f"https://www.youtube.com/playlist?list={pl1}": playlist_info(
            pl1, [flat_video("a" * 11), flat_video("b" * 11, "[Private video]")]
        ),
        "https://www.youtube.com/@doc/videos": tab_info([flat_video("a" * 11), flat_video("c" * 11)]),
        "https://www.youtube.com/@doc/shorts": tab_info([flat_video("c" * 11), flat_video("d" * 11)], "Shorts"),
    }
    yt = StubYouTube(table, YouTubeConfig(channel_tabs=["videos", "shorts", "streams"]))
    assert [(p.id, p.title) for p in yt.list_playlists("https://www.youtube.com/@doc")] == [(pl1, "A"), (pl2, "B")]
    vids = yt.list_playlist_videos(f"https://www.youtube.com/playlist?list={pl1}")
    assert [(v.id, v.available) for v in vids] == [("a" * 11, True), ("b" * 11, False)]
    # Missing tabs (streams) are skipped with a warning; videos are deduplicated across tabs.
    assert [v.id for v in yt.list_channel_videos("https://www.youtube.com/@doc")] == ["a" * 11, "c" * 11, "d" * 11]
    assert yt.list_playlists("https://www.youtube.com/@nobody") == []


def test_resolve_playlist_and_video() -> None:
    pid = "PL" + "3" * 32
    yt = StubYouTube(
        {
            f"https://www.youtube.com/playlist?list={pid}": playlist_info(pid, [flat_video("a" * 11)]),
            "https://www.youtube.com/playlist?list=RDxyz": playlist_info("RDxyz", [flat_video("a" * 11)], owner=False),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": video_page("dQw4w9WgXcQ"),
            "https://www.youtube.com/watch?v=nochannel00": {"id": "nochannel00", "title": "orphan"},
        }
    )
    details = yt.resolve_playlist(pid)
    assert (
        details.playlist.title == "Сердце" and details.channel.id == UC and [v.id for v in details.videos] == ["a" * 11]
    )
    assert yt.resolve_playlist("RDxyz").channel is None

    video = yt.resolve_video("dQw4w9WgXcQ")
    assert video.channel.id == UC and video.video.upload_date == "2024-01-15" and video.video.duration == 3723
    assert yt.requests[-1] == ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", False), (
        "one video page is a full extraction"
    )
    expect_raises(YouTubeError, yt.resolve_video, "nochannel00", contains="nochannel00")
    expect_raises(YouTubeError, yt.resolve_video, "missing0000")


# -------------------------------------------------------------------- recorded fixtures (optional)
def test_recorded_fixtures() -> None:
    """Parse every recorded yt-dlp response in tests/fixtures; silently passes when none were recorded."""
    files = sorted(FIXTURES.glob("*.json")) if FIXTURES.exists() else []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        kind, info = data["kind"], data["info"]
        if kind == "channel_videos":
            videos = _videos_from_info(info)
            assert videos and all(len(v.id) == 11 and v.url.endswith(v.id) for v in videos), path.name
            assert _channel_from_info(info, allow_own_id=True) is not None, f"{path.name}: no channel id"
        elif kind == "channel_playlists":
            playlists = [_playlist_from_entry(e) for e in _iter_entries(info)]
            assert any(playlists), f"{path.name}: no playlists parsed"
            assert all(p.id.startswith(("PL", "OL", "UU", "FL", "RD")) or len(p.id) > 11 for p in playlists if p), (
                path.name
            )
        elif kind == "playlist":
            assert _channel_from_info(info) is not None, f"{path.name}: playlist without owner channel"
            assert _videos_from_info(info), f"{path.name}: playlist without videos"
        elif kind == "video":
            v = _video_from_entry(info)
            assert v is not None and v.duration and v.upload_date, f"{path.name}: incomplete video metadata"
            assert _channel_from_info(info) is not None, f"{path.name}: video without channel"
        else:
            raise AssertionError(f"{path.name}: unknown fixture kind {kind!r}")
    print(f"      ({len(files)} recorded fixture(s))")


if __name__ == "__main__":
    run_tests(globals())
