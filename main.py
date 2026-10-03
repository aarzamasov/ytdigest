#!/usr/bin/env python3
"""ytt — YouTube channel / playlist / video -> audio -> text -> Markdown digests.

Usage:
    python main.py run     <target>           full pipeline: sync + process + build (safe to re-run)
    python main.py sync    <target>           discover playlists/videos only
    python main.py process <target>           download + transcribe pending videos, then build
    python main.py build   <target> [--force] (re)build Markdown digests from existing transcripts
    python main.py status  <target>           progress overview

<target> is detected automatically:
    channel   — channel URL, @handle or UC... id          -> all playlists + videos outside playlists
    playlist  — playlist URL or PL... id                  -> only that playlist
    video     — watch / youtu.be / shorts / live URL      -> only that video (filed under "no playlist"
                                                            until a full channel sync places it)
Force the interpretation with --channel / --playlist / --video, e.g. `--channel <video URL>`
means "the whole channel this video belongs to".
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ytt.config import Config
from ytt.db import Database
from ytt.pipeline import Pipeline
from ytt.util import setup_logging
from ytt.youtube import Kind, Target, detect_target

log = logging.getLogger("ytt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ytt", description="YouTube channel / playlist / video -> transcripts -> Markdown digests"
    )
    parser.add_argument("-c", "--config", help="path to config.toml (default: <repo>/config.toml if present)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug output in the console")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_target(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("target", help="channel URL / @handle / UC id, playlist URL / PL id, or video URL")
        kind = p.add_mutually_exclusive_group()
        kind.add_argument(
            "--channel",
            dest="kind",
            action="store_const",
            const=Kind.CHANNEL,
            help="treat the target as a channel (on a video/playlist URL: the channel it belongs to)",
        )
        kind.add_argument(
            "--playlist",
            dest="kind",
            action="store_const",
            const=Kind.PLAYLIST,
            help="treat the target as a playlist (e.g. a watch?v=...&list=... URL)",
        )
        kind.add_argument(
            "--video",
            dest="kind",
            action="store_const",
            const=Kind.VIDEO,
            help="treat the target as a single video (also accepts a bare 11-char id)",
        )
        return p

    def with_process_options(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--limit", type=int, help="process at most N videos in this run (handy for a test drive)")
        p.add_argument("--retry-errors", action="store_true", help="reset attempt counters of failed videos")
        redo_grp = p.add_mutually_exclusive_group()
        redo_grp.add_argument(
            "--redo",
            action="store_true",
            help="re-transcribe video (reuses existing audio if present; single video only)",
        )
        redo_grp.add_argument(
            "--redo-audio",
            action="store_true",
            help="re-download audio and re-transcribe (single video only)",
        )
        return p

    with_process_options(with_target(sub.add_parser("run", help="sync + process + build")))
    with_target(sub.add_parser("sync", help="discover playlists and videos, no downloads"))
    with_process_options(with_target(sub.add_parser("process", help="download + transcribe + build (no sync)")))
    build = with_target(sub.add_parser("build", help="(re)build Markdown digests"))
    build.add_argument("--force", action="store_true", help="rewrite even if nothing changed")
    with_target(sub.add_parser("status", help="show progress"))
    return parser


def scope(target: Target) -> dict[str, str | None]:
    """Narrowing for process/build: a playlist target works on that playlist, a video target on that video."""
    return {
        "only_playlist": target.playlist_id if target.kind == Kind.PLAYLIST else None,
        "only_video": target.video_id if target.kind == Kind.VIDEO else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    data_dir = Path(cfg.paths.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir / cfg.paths.log_file, verbose=args.verbose)

    try:
        target = detect_target(args.target, args.kind)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    if (getattr(args, "redo", False) or getattr(args, "redo_audio", False)) and target.kind != Kind.VIDEO:
        log.error("--redo and --redo-audio can only be used with a single video target")
        return 2

    log.info("Target: %s", target.label)
    if target.note:
        log.info("Note: %s", target.note)
    narrowing = scope(target)

    db = Database(data_dir / cfg.paths.db_file)
    pipeline = Pipeline(cfg, db)
    try:
        if args.command == "run":
            channel = pipeline.sync(target)
            pipeline.process(
                channel,
                limit=args.limit,
                retry_errors=args.retry_errors,
                redo=args.redo,
                redo_audio=args.redo_audio,
                **narrowing,
            )
            print("\n" + pipeline.status(channel, video_id=narrowing["only_video"]))
        elif args.command == "sync":
            channel = pipeline.sync(target)
            print("\n" + pipeline.status(channel, video_id=narrowing["only_video"]))
        elif args.command == "process":
            channel = pipeline.resolve_channel(target, online=False)
            pipeline.process(
                channel,
                limit=args.limit,
                retry_errors=args.retry_errors,
                redo=args.redo,
                redo_audio=args.redo_audio,
                **narrowing,
            )
            print("\n" + pipeline.status(channel, video_id=narrowing["only_video"]))
        elif args.command == "build":
            channel = pipeline.resolve_channel(target, online=False)
            pipeline.build(channel, force=args.force, only_playlist=narrowing["only_playlist"])
        elif args.command == "status":
            channel = pipeline.resolve_channel(target, online=False)
            print(pipeline.status(channel, video_id=narrowing["only_video"]))
    except KeyboardInterrupt:
        log.warning("Interrupted. Progress is saved in the database — run the same command again to continue.")
        return 130
    except Exception as exc:  # noqa: BLE001 — show a clean message instead of a traceback
        log.error("%s", exc)
        log.debug("traceback", exc_info=True)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
