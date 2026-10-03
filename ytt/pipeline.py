"""Orchestration: sync (discover) -> process (download + transcribe) -> build (Markdown digests).

The input can be a whole channel, one playlist or one video (see `youtube.detect_target`).
Playlist and video runs touch only what they were given; digests are always rebuilt from
everything the database knows about the channel, and only when something changed.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .builder import channel_fingerprint, playlist_fingerprint, render_channel_md, render_playlist_md
from .config import Config
from .db import Database, Status
from .downloader import Downloader
from .storage import Layout, safe_name
from .transcriber import Transcriber, get_transcriber, segments_to_text
from .util import fmt_duration, utcnow_iso
from .youtube import ChannelInfo, Kind, Target, VideoInfo, YouTube, normalize_channel_url

log = logging.getLogger(__name__)

UNSORTED_PREFIX = "__unsorted__"
UNSORTED_POSITION = 1_000_000  # the virtual playlist always sorts after the real ones

Row = Any  # sqlite3.Row


def unsorted_playlist_id(channel_id: str) -> str:
    """Id of the virtual playlist that collects channel videos not present in any real playlist."""
    return f"{UNSORTED_PREFIX}{channel_id}"


class Pipeline:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.yt = YouTube(cfg.youtube)
        self.downloader = Downloader(cfg.download)
        self._transcriber: Transcriber | None = None
        self._ready = False

    # ------------------------------------------------------------- channel lookup
    def resolve_channel(self, target: Target, online: bool) -> Row:
        """Channel row for a target of any kind. Offline mode answers from the DB whenever it can."""
        if not online:
            row = self._find_channel_offline(target)
            if row is not None:
                return row
            if target.kind != Kind.CHANNEL:
                raise LookupError(f"{target.label} is not in the database yet — run `sync` or `run` with it first")
        return self._register_channel(self._lookup_channel(target))

    def _find_channel_offline(self, target: Target) -> Row | None:
        if target.kind == Kind.VIDEO:
            video = self.db.get_video(target.video_id or "")
            return self.db.get_channel(video["channel_id"]) if video else None
        if target.kind == Kind.PLAYLIST:
            playlist = self.db.get_playlist(target.playlist_id or "")
            return self.db.get_channel(playlist["channel_id"]) if playlist else None
        if target.channel:
            key = target.channel
            if key.startswith("UC") and len(key) == 24:
                return self.db.get_channel(key)
            return self.db.find_channel(normalize_channel_url(key))
        # `--channel` given a playlist/video URL: the channel that thing was filed under.
        if target.playlist_id and (playlist := self.db.get_playlist(target.playlist_id)):
            return self.db.get_channel(playlist["channel_id"])
        if target.video_id and (video := self.db.get_video(target.video_id)):
            return self.db.get_channel(video["channel_id"])
        return None

    def _lookup_channel(self, target: Target) -> ChannelInfo:
        """Ask YouTube which channel the target belongs to."""
        if target.channel:
            return self.yt.resolve_channel(target.channel)
        if target.playlist_id:
            details = self.yt.resolve_playlist(target.playlist_id)
            if details.channel is None:
                raise LookupError(f"playlist {target.playlist_id} has no owner channel (a mix or a system playlist?)")
            return details.channel
        return self.yt.resolve_video(target.video_id or "").channel

    def _register_channel(self, info: ChannelInfo) -> Row:
        existing = self.db.get_channel(info.id)
        dir_name = (
            existing["dir_name"] if existing else self._unique_name(info.name, self.db.channel_dir_names(), info.id)
        )
        self.db.upsert_channel(info.id, info.url, info.name, dir_name, utcnow_iso())
        return self._channel_row(info.id)

    def _channel_row(self, channel_id: str) -> Row:
        """A channel that must exist (it was just registered or is referenced by a stored row)."""
        row = self.db.get_channel(channel_id)
        if row is None:
            raise LookupError(f"channel {channel_id} is missing from the database")
        return row

    # ----------------------------------------------------------------------- sync
    def sync(self, target: Target) -> Row:
        """Discover and store metadata without downloading anything; the scope follows the target kind."""
        if target.kind == Kind.PLAYLIST:
            return self._sync_playlist(target.playlist_id or "")
        if target.kind == Kind.VIDEO:
            return self._sync_video(target.video_id or "")
        return self._sync_channel(self.resolve_channel(target, online=True))

    def _sync_channel(self, ch: Row) -> Row:
        """Full channel sync: every playlist, every video; videos outside playlists -> virtual playlist."""
        cid, now = ch["id"], utcnow_iso()
        log.info("Channel: %s (%s)", ch["name"], ch["url"])
        playlists_before, videos_before = self.db.entity_counts(cid)

        playlists = self.yt.list_playlists(ch["url"])
        log.info("Playlists found: %d", len(playlists))
        seen_ids: list[str] = []
        for pos, pl in enumerate(playlists):
            dir_name = self.db.playlist_dir_name(pl.id) or self._unique_name(
                pl.title, self.db.playlist_dir_names(cid), pl.id
            )
            self.db.upsert_playlist(cid, pl.id, pl.title, pl.url, pos, dir_name, False, now)
            seen_ids.append(pl.id)
            videos = self.yt.list_playlist_videos(pl.url)
            self._upsert_videos(cid, videos, now)
            self.db.replace_membership(pl.id, [(v.id, i) for i, v in enumerate(videos)])
            log.info("  [%d/%d] %s — %d videos", pos + 1, len(playlists), pl.title, len(videos))

        removed = self.db.deactivate_missing_playlists(cid, seen_ids)
        if removed:
            log.info("Playlists no longer on the channel (kept in DB, excluded from output): %d", removed)

        # Everything on the channel that is not in any (active, real) playlist -> virtual playlist.
        channel_videos = self.yt.list_channel_videos(ch["url"])
        self._upsert_videos(cid, channel_videos, now)
        in_playlists = self.db.video_ids_in_real_playlists(cid)
        unsorted = [v.id for v in channel_videos if v.id not in in_playlists]
        # Videos added by their own URL may be unlisted (absent from the channel tabs) — keep them.
        unsorted += [vid for vid in self.db.manual_video_ids(cid) if vid not in in_playlists and vid not in unsorted]
        upid = self._ensure_unsorted_playlist(ch, now)
        self.db.replace_membership(upid, [(vid, i) for i, vid in enumerate(unsorted)])
        log.info("Channel videos: %d, not in any playlist: %d", len(channel_videos), len(unsorted))

        self.db.touch_channel_synced(cid, now)
        self._verify_transcripts(cid)

        playlists_after, videos_after = self.db.entity_counts(cid)
        log.info(
            "Sync done: %d new playlists, %d new videos",
            playlists_after - playlists_before,
            videos_after - videos_before,
        )
        return self.db.get_channel(cid)

    def _sync_playlist(self, playlist_id: str) -> Row:
        """One playlist by its own URL: register its channel if needed, store the playlist and its videos."""
        details = self.yt.resolve_playlist(playlist_id)
        pl, videos = details.playlist, details.videos
        existing = self.db.get_playlist(pl.id)
        if existing is not None:
            ch = self._channel_row(existing["channel_id"])  # stays with the channel it was first filed under
        elif details.channel is None:
            raise LookupError(f"playlist {playlist_id} has no owner channel (a mix or a system playlist?)")
        else:
            ch = self._register_channel(details.channel)
        cid, now = ch["id"], utcnow_iso()
        log.info("Playlist: %s (%s) — channel %s", pl.title, pl.url, ch["name"])
        _, videos_before = self.db.entity_counts(cid)

        position = existing["position"] if existing else self.db.next_playlist_position(cid)
        dir_name = (
            existing["dir_name"] if existing else self._unique_name(pl.title, self.db.playlist_dir_names(cid), pl.id)
        )
        self.db.upsert_playlist(cid, pl.id, pl.title, pl.url, position, dir_name, False, now, manual=True)
        self._upsert_videos(cid, videos, now)
        self.db.replace_membership(pl.id, [(v.id, i) for i, v in enumerate(videos)])
        # These videos have a real home now; drop them from the virtual "unsorted" playlist if they were there.
        moved = self.db.remove_membership(unsorted_playlist_id(cid), [v.id for v in videos])
        self._verify_transcripts(cid)

        _, videos_after = self.db.entity_counts(cid)
        note = f", {moved} moved out of '{self.cfg.youtube.unsorted_playlist_title}'" if moved else ""
        log.info("Playlist synced: %d videos, %d new%s", len(videos), videos_after - videos_before, note)
        return self.db.get_channel(cid)

    def _sync_video(self, video_id: str) -> Row:
        """One video by its own URL: register its channel if needed and file the video."""
        details = self.yt.resolve_video(video_id)
        existing = self.db.get_video(video_id)
        ch = self._channel_row(existing["channel_id"]) if existing else self._register_channel(details.channel)
        cid, now = ch["id"], utcnow_iso()
        log.info("Video: %s (%s) — channel %s", details.video.title, details.video.url, ch["name"])

        self._upsert_videos(cid, [details.video], now, manual=True)
        if not self.db.video_playlist_ids(video_id):
            # YouTube does not tell which playlists contain a video, so on its own it goes to the virtual
            # playlist; a later full channel sync moves it to its real playlist(s), the transcript stays put.
            upid = self._ensure_unsorted_playlist(ch, now)
            self.db.add_membership(upid, video_id)
            log.info("Not in any known playlist — filed under '%s'", self.cfg.youtube.unsorted_playlist_title)
        row = self.db.get_video(video_id)
        if row is not None and row["status"] == Status.SKIPPED:
            log.info("Video is skipped: %s", row["error"])
        self._verify_transcripts(cid)
        return self._channel_row(cid)

    def _ensure_unsorted_playlist(self, ch: Row, now: str) -> str:
        cid = ch["id"]
        upid = unsorted_playlist_id(cid)
        title = self.cfg.youtube.unsorted_playlist_title
        dir_name = self.db.playlist_dir_name(upid) or self._unique_name(title, self.db.playlist_dir_names(cid), cid)
        self.db.upsert_playlist(cid, upid, title, f"{ch['url']}/videos", UNSORTED_POSITION, dir_name, True, now)
        return upid

    def _upsert_videos(self, channel_id: str, videos: list[VideoInfo], now: str, manual: bool = False) -> None:
        for v in videos:
            status, error = Status.PENDING, None
            if not v.available:
                status, error = Status.SKIPPED, "unavailable (private or deleted)"
            elif v.is_live and self.cfg.youtube.skip_live:
                status, error = Status.SKIPPED, f"live stream ({v.live_status})"
            self.db.upsert_video(channel_id, v, status, error, now, manual=manual)

    def _verify_transcripts(self, channel_id: str) -> None:
        """If someone deleted a transcript file, queue the video again instead of producing holes."""
        for row in self.db.done_videos(channel_id):
            path = row["transcript_path"]
            if not path or not Path(path).exists():
                log.warning("Transcript file missing for %s (%s) — queued again", row["id"], row["title"])
                self.db.reset_video(row["id"])

    def _unique_name(self, title: str, taken: set[str], uid: str) -> str:
        base = safe_name(title, self.cfg.output.max_name_len)
        return base if base not in taken else f"{base} [{uid[-8:]}]"

    # -------------------------------------------------------------------- process
    def process(
        self,
        ch: Row,
        limit: int | None = None,
        retry_errors: bool = False,
        only_playlist: str | None = None,
        only_video: str | None = None,
    ) -> None:
        """Playlist by playlist: download + transcribe every unprocessed video, rebuild digests on change.

        `only_playlist` / `only_video` narrow the work to one playlist or one video (the playlist and
        video input modes); the channel digest is still refreshed if anything changed.
        """
        cid = ch["id"]
        layout = Layout(self.cfg, ch["dir_name"])
        playlists = self._scoped_playlists(cid, only_playlist, only_video)
        if retry_errors:
            scope = None
            if only_video is not None:
                scope = [only_video]
            elif only_playlist is not None:
                scope = [r["id"] for r in self.db.playlist_videos(only_playlist)]
            log.info("Reset %d errored videos for another attempt", self.db.reset_errors(cid, scope))

        processed = 0
        for pl in playlists:
            rows = self.db.playlist_videos(pl["id"])
            if only_video is not None:
                rows = [r for r in rows if r["id"] == only_video]
            todo = [r for r in rows if self._needs_work(r)]
            if todo:
                log.info("Playlist '%s': %d of %d videos to process", pl["title"], len(todo), len(rows))

            limit_hit = False
            for row in rows:
                if not self._needs_work(row):
                    continue  # done earlier (possibly under another playlist), skipped, or out of attempts
                if limit is not None and processed >= limit:
                    limit_hit = True
                    break
                self._ensure_ready()
                self._process_video(row, pl, layout)
                processed += 1

            self._build_playlist(ch, pl["id"], layout, force=False)
            if limit_hit:
                log.info("Limit of %d videos reached", limit)
                break

        self._build_channel(ch, layout, force=False)
        log.info("Processed %d videos in this run", processed)

    def _scoped_playlists(self, channel_id: str, only_playlist: str | None, only_video: str | None) -> list[Row]:
        playlists = self.db.active_playlists(channel_id)
        if only_playlist is not None:
            playlists = [p for p in playlists if p["id"] == only_playlist]
        if only_video is not None:
            homes = set(self.db.video_playlist_ids(only_video))
            playlists = [p for p in playlists if p["id"] in homes]
        return playlists

    def _needs_work(self, row: Row) -> bool:
        return row["status"] not in (Status.DONE, Status.SKIPPED) and row["attempts"] < self.cfg.download.max_attempts

    def _ensure_ready(self) -> None:
        """Check the download tool and load the model once, before the first video (fail fast)."""
        if self._ready:
            return
        self.downloader.check()
        _ = self.transcriber
        self._ready = True

    @property
    def transcriber(self) -> Transcriber:
        if self._transcriber is None:
            t0 = time.time()
            log.info("Loading model %s (%s)...", self.cfg.transcribe.model, self.cfg.transcribe.backend)
            self._transcriber = get_transcriber(self.cfg.transcribe)
            log.info("Model ready in %.0fs", time.time() - t0)
        return self._transcriber

    def _process_video(self, row: Row, playlist: Row, layout: Layout) -> None:
        vid, title = row["id"], row["title"]
        log.info("> %s  [%s, %s]", title, vid, fmt_duration(row["duration"]))
        self.db.bump_attempts(vid)
        try:
            t0 = time.time()
            audio = self.downloader.download(row["url"], vid, layout.audio_dir)
            self.db.set_audio(vid, str(audio))
            log.info("  audio ready: %s (%.0fs)", audio.name, time.time() - t0)

            t1 = time.time()
            segments = self.transcriber.transcribe(audio)
            tc = self.cfg.transcribe
            text = segments_to_text(
                segments,
                gap_sec=tc.paragraph_gap_sec,
                max_chars=tc.paragraph_max_chars,
                timestamps=tc.timestamps,
                chunk_sec=tc.chunk_sec,
            )
            if not text.strip():
                raise RuntimeError("empty transcript (no speech detected?)")
            log.info("  transcribed: %d chars in %.0fs", len(text), time.time() - t1)

            # The transcript lives in the folder of the playlist it was first processed under.
            path = layout.transcript_path(playlist["dir_name"], row["position"] + 1, title, vid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            self.db.mark_done(vid, str(path), playlist["id"], utcnow_iso())
            log.info("  saved: %s", path.relative_to(layout.root))

            if not self.cfg.download.keep_audio:
                audio.unlink(missing_ok=True)
                self.db.set_audio(vid, None)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad video must not stop the run
            log.error("  FAILED: %s", exc)
            log.debug("traceback", exc_info=True)
            self.db.mark_error(vid, str(exc)[:1000])

    # ---------------------------------------------------------------------- build
    def build(self, ch: Row, force: bool = False, only_playlist: str | None = None) -> None:
        """(Re)build digests from existing transcripts; with force=False only changed ones are rewritten."""
        layout = Layout(self.cfg, ch["dir_name"])
        playlists = self._scoped_playlists(ch["id"], only_playlist, None)
        updated = sum(self._build_playlist(ch, pl["id"], layout, force) for pl in playlists)
        channel_updated = self._build_channel(ch, layout, force)
        log.info(
            "Build: %d playlist digests updated, channel digest %s",
            updated,
            "updated" if channel_updated else "unchanged",
        )

    def _build_playlist(self, ch: Row, playlist_id: str, layout: Layout, force: bool) -> bool:
        pl = self.db.get_playlist(playlist_id)
        rows = self.db.playlist_videos(playlist_id)
        if pl is None or not rows:
            return False
        fingerprint = playlist_fingerprint(pl, rows, self._render_key())
        md_path = layout.playlist_md_path(pl["dir_name"])
        if not force and fingerprint == pl["fingerprint"] and md_path.exists():
            return False
        now = utcnow_iso()
        markdown = render_playlist_md(
            ch,
            pl,
            rows,
            self._read_transcript,
            built_at=now,
            playlist_titles=self.db.playlist_titles(ch["id"]),
            dedupe_foreign=self.cfg.output.dedupe_in_playlist_files,
            membership=self.db.membership_pairs(ch["id"]) if self.cfg.output.dedupe_in_playlist_files else None,
            timestamp_links=self.cfg.output.timestamp_links,
        )
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        self.db.set_playlist_build(playlist_id, fingerprint, str(md_path), now)
        log.info("Digest written: %s", md_path.relative_to(layout.root))
        return True

    def _build_channel(self, ch: Row, layout: Layout, force: bool) -> bool:
        ch = self.db.get_channel(
            ch["id"]
        )  # refresh: fingerprint / sync state may have changed since the caller fetched it
        bundles = []
        for pl in self.db.active_playlists(ch["id"]):
            rows = self.db.playlist_videos(pl["id"])
            if rows:
                bundles.append((pl, rows))
        if not bundles:
            return False
        fingerprint = channel_fingerprint(ch, bundles, self._render_key())
        md_path = layout.channel_md_path()
        if not force and fingerprint == ch["fingerprint"] and md_path.exists():
            return False
        now = utcnow_iso()
        markdown = render_channel_md(
            ch, bundles, self._read_transcript, built_at=now, timestamp_links=self.cfg.output.timestamp_links
        )
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        self.db.set_channel_build(ch["id"], fingerprint, str(md_path), now)
        log.info("Channel digest written: %s", md_path)
        return True

    def _render_key(self) -> str:
        """Output options that change the rendered Markdown; part of the fingerprints so a config
        change triggers a rebuild on the next run."""
        o = self.cfg.output
        return f"links={int(o.timestamp_links)};dedupe={int(o.dedupe_in_playlist_files)}"

    @staticmethod
    def _read_transcript(row: Row) -> str | None:
        path = row["transcript_path"]
        if not path:
            return None
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            return None

    # --------------------------------------------------------------------- status
    def status(self, ch: Row, video_id: str | None = None) -> str:
        cid = ch["id"]
        counts = self.db.status_counts(cid)
        total = sum(counts.values())
        layout = Layout(self.cfg, ch["dir_name"])
        lines = [
            f"Channel : {ch['name']}  ({ch['url']})",
            f"Videos  : {total} total — " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())),
            f"Synced  : {ch['last_synced'] or 'never as a whole (only playlists/videos added one by one)'}",
            f"Output  : {layout.output_dir}",
            "",
            "Playlists (done/total):",
        ]
        for p in self.db.playlist_stats(cid):
            extra = []
            if p["errors"]:
                extra.append(f"errors: {p['errors']}")
            if p["skipped"]:
                extra.append(f"skipped: {p['skipped']}")
            suffix = f"  ({', '.join(extra)})" if extra else ""
            lines.append(f"  [{p['done']:>3}/{p['total']:<3}] {p['title']}{suffix}")
        errors = self.db.errored_videos(cid)
        if errors:
            lines += ["", "Recent errors:"]
            for e in errors:
                lines.append(f"  {e['id']}  {str(e['title'])[:50]:<50}  (attempt {e['attempts']}) {e['error']}")
        if video_id and (v := self.db.get_video(video_id)):
            detail = f" — {v['error']}" if v["error"] else ""
            lines += ["", f"Video   : {v['title']}  ({v['url']})", f"Status  : {v['status']}{detail}"]
            if v["transcript_path"]:
                lines.append(f"Text    : {v['transcript_path']}")
        return "\n".join(lines)
