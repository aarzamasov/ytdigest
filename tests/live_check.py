#!/usr/bin/env python3
"""Opt-in live check against the real YouTube (needs network, yt-dlp and, for --transcribe, ffmpeg plus a
Whisper backend). Nothing here touches your real data/ folder: everything goes to a temporary directory.

    python tests/live_check.py @channel                 # discovery only: playlists, videos, one video page
    python tests/live_check.py @channel --transcribe    # also download + transcribe the shortest video (tiny model)
    python tests/live_check.py @channel --cookies-from-browser chrome

Exit code 0 means every sanity check passed; the printed counts tell you what yt-dlp returned.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ytt.config import Config  # noqa: E402
from ytt.db import Database  # noqa: E402
from ytt.pipeline import Pipeline  # noqa: E402
from ytt.util import setup_logging  # noqa: E402
from ytt.youtube import YouTube, detect_target  # noqa: E402


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channel", help="channel URL, @handle or UC... id")
    parser.add_argument(
        "--transcribe", action="store_true", help="also run download + transcription on the shortest video"
    )
    parser.add_argument("--cookies-from-browser", default="", help="chrome | firefox | safari | edge")
    parser.add_argument("--model", default="tiny", help="Whisper model for --transcribe (default tiny: fast, rough)")
    args = parser.parse_args()

    cfg = Config()
    cfg.youtube.cookies_from_browser = args.cookies_from_browser
    cfg.transcribe.model = args.model
    yt = YouTube(cfg.youtube)

    t0 = time.time()
    channel = yt.resolve_channel(args.channel)
    print(f"channel: {channel.name}  {channel.url}  ({channel.id})")
    check(channel.id.startswith("UC") and len(channel.id) == 24, "channel id looks like UC...")

    playlists = yt.list_playlists(channel.url)
    print(f"playlists: {len(playlists)}")
    for pl in playlists[:5]:
        print(f"  - {pl.title}  ({pl.id})")
    videos = yt.list_channel_videos(channel.url)
    print(f"videos on the channel tabs {cfg.youtube.channel_tabs}: {len(videos)}")
    for v in videos[:5]:
        print(f"  - {v.title}  [{v.id}] {v.duration}s {v.upload_date} live={v.live_status} avail={v.available}")
    check(bool(videos), "at least one video found (if not: pip install -U yt-dlp, or set --cookies-from-browser)")
    check(all(len(v.id) == 11 for v in videos), "video ids are 11 characters")
    check(sum(1 for v in videos if v.duration) >= len(videos) * 0.8, "durations present for most videos")
    check(
        sum(1 for v in videos if v.upload_date) >= len(videos) * 0.5,
        "upload dates present (approximate_date) for most videos",
    )

    if playlists:
        details = yt.resolve_playlist(playlists[0].id)
        print(
            f"first playlist '{details.playlist.title}': {len(details.videos)} videos, owner={details.channel.id if details.channel else None}"
        )
        check(
            details.channel is not None and details.channel.id == channel.id, "playlist owner resolves to the channel"
        )
        check(bool(details.videos), "playlist has videos")
    else:
        print("  (no playlists on this channel — playlist checks skipped)")

    first = next((v for v in videos if v.available and not v.is_live), None)
    if first is not None:
        page = yt.resolve_video(first.id)
        print(
            f"video page: {page.video.title}  {page.video.duration}s  {page.video.upload_date}  live={page.video.live_status}"
        )
        check(page.channel.id == channel.id, "video page names the same channel")
        check(bool(page.video.duration) and bool(page.video.upload_date), "video page has duration and upload date")
    print(f"discovery checks passed in {time.time() - t0:.0f}s")

    if args.transcribe:
        candidates = [v for v in videos if v.available and not v.is_live and v.duration]
        check(bool(candidates), "a processable video exists for --transcribe")
        shortest = min(candidates, key=lambda v: v.duration or 0)
        print(f"\ntranscribing the shortest video: {shortest.title} ({shortest.duration}s) with model '{args.model}'")
        tmp = Path(tempfile.mkdtemp(prefix="ytt-live-"))
        cfg.paths.data_dir = str(tmp)
        setup_logging(None)
        db = Database(tmp / cfg.paths.db_file)
        try:
            pipeline = Pipeline(cfg, db)
            target = detect_target(shortest.url)
            ch = pipeline.sync(target)
            pipeline.process(ch, only_video=shortest.id)
            row = db.get_video(shortest.id)
            check(row["status"] == "done", f"video processed (status={row['status']}, error={row['error']})")
            text = Path(row["transcript_path"]).read_text(encoding="utf-8")
            print(f"transcript: {len(text)} chars, first line: {text.splitlines()[0][:120]!r}")
            check(len(text) > 20, "transcript is not empty")
        finally:
            db.close()
            shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL LIVE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
