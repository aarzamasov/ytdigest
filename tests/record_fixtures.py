#!/usr/bin/env python3
"""Record real yt-dlp responses as fixtures for tests/test_youtube_parsing.py.

    python tests/record_fixtures.py @channel [--max-entries 5]

Needs network access and yt-dlp. Writes tests/fixtures/<channel>-<kind>.json for:
channel_videos (the Videos tab), channel_playlists (the Playlists tab), playlist (the first playlist),
video (the first video, full extraction). Bulky keys (formats, thumbnails, subtitles, ...) are dropped and
entry lists are truncated, so the files stay small enough to commit. Re-run after a yt-dlp upgrade to
see whether the shape of the metadata changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ytt.config import YouTubeConfig  # noqa: E402
from ytt.storage import safe_name  # noqa: E402
from ytt.youtube import YouTube, _videos_from_info  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DROP_KEYS = {
    "formats",
    "requested_formats",
    "requested_downloads",
    "thumbnails",
    "thumbnail",
    "automatic_captions",
    "subtitles",
    "heatmap",
    "chapters",
    "description",
    "tags",
    "categories",
    "http_headers",
    "_format_sort_fields",
    "__post_extractor",
    "__postprocessors",
    "comments",
    "epoch",
    "_version",
}


def slim(obj: Any, max_entries: int) -> Any:
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in DROP_KEYS:
                continue
            if key == "entries" and value is not None:
                value = list(value)[:max_entries]
            out[key] = slim(value, max_entries)
        return out
    if isinstance(obj, (list, tuple)):
        return [slim(item, max_entries) for item in obj]
    return obj


def save(name: str, kind: str, info: dict, max_entries: int) -> None:
    FIXTURES.mkdir(exist_ok=True)
    path = FIXTURES / f"{name}-{kind}.json"
    path.write_text(
        json.dumps({"kind": kind, "info": slim(info, max_entries)}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    print(f"wrote {path.relative_to(FIXTURES.parent.parent)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channel", help="channel URL, @handle or UC... id")
    parser.add_argument("--max-entries", type=int, default=5, help="keep at most N entries per list (default 5)")
    parser.add_argument(
        "--cookies-from-browser", default="", help="chrome | firefox | safari | edge, if YouTube asks to sign in"
    )
    args = parser.parse_args()

    yt = YouTube(YouTubeConfig(cookies_from_browser=args.cookies_from_browser))
    channel = yt.resolve_channel(args.channel)
    name = safe_name(channel.url.rsplit("/", 1)[-1].lstrip("@") or channel.id, 40)
    print(f"channel: {channel.name} ({channel.url})")

    videos_tab = yt._extract(f"{channel.url}/videos")
    save(name, "channel_videos", videos_tab, args.max_entries)
    playlists_tab = yt._extract(f"{channel.url}/playlists")
    save(name, "channel_playlists", playlists_tab, args.max_entries)

    playlists = yt.list_playlists(channel.url)
    if playlists:
        save(name, "playlist", yt._extract(playlists[0].url), args.max_entries)
    else:
        print("no playlists on the channel — playlist fixture skipped")

    videos = _videos_from_info(videos_tab)
    if videos:
        save(name, "video", yt._extract(videos[0].url, flat=False), args.max_entries)
    else:
        print("no videos on the Videos tab — video fixture skipped")
    print("done; run: python tests/test_youtube_parsing.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
