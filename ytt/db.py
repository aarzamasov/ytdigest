"""SQLite persistence for channels, playlists, videos and playlist membership."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .youtube import VideoInfo


class Status:
    PENDING = "pending"  # discovered, nothing done yet
    DOWNLOADED = "downloaded"  # audio is on disk, transcription not finished
    DONE = "done"  # transcript written to disk
    ERROR = "error"  # last attempt failed; retried on next run while attempts < max_attempts
    SKIPPED = "skipped"  # private/deleted/live — not processed (re-checked on every sync)


SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    name        TEXT NOT NULL,
    dir_name    TEXT NOT NULL,
    fingerprint TEXT,
    md_path     TEXT,
    built_at    TEXT,
    first_seen  TEXT NOT NULL,
    last_synced TEXT              -- last FULL channel sync; NULL while only playlists/videos were added one by one
);

CREATE TABLE IF NOT EXISTS playlists (
    id          TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL REFERENCES channels(id),
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    position    INTEGER NOT NULL,
    dir_name    TEXT NOT NULL,
    is_virtual  INTEGER NOT NULL DEFAULT 0,
    manual      INTEGER NOT NULL DEFAULT 0,  -- added by its own URL: kept even if absent from the channel's Playlists tab
    active      INTEGER NOT NULL DEFAULT 1,
    fingerprint TEXT,
    md_path     TEXT,
    built_at    TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    id               TEXT PRIMARY KEY,
    channel_id       TEXT NOT NULL REFERENCES channels(id),
    title            TEXT NOT NULL,
    url              TEXT NOT NULL,
    duration         INTEGER,
    upload_date      TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    error            TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    audio_path       TEXT,
    transcript_path  TEXT,
    home_playlist_id TEXT,
    transcribed_at   TEXT,
    manual           INTEGER NOT NULL DEFAULT 0,  -- added by its own URL: kept even if absent from the channel tabs
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_videos (
    playlist_id TEXT NOT NULL REFERENCES playlists(id),
    video_id    TEXT NOT NULL REFERENCES videos(id),
    position    INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_videos_channel_status ON videos (channel_id, status);
CREATE INDEX IF NOT EXISTS idx_playlist_videos_video ON playlist_videos (video_id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ channels
    def upsert_channel(self, channel_id: str, url: str, name: str, dir_name: str, now: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO channels (id, url, name, dir_name, first_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET url = excluded.url, name = excluded.name
                """,
                (channel_id, url, name, dir_name, now),
            )

    def touch_channel_synced(self, channel_id: str, now: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE channels SET last_synced = ? WHERE id = ?", (now, channel_id))

    def get_channel(self, channel_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()

    def find_channel(self, url_or_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM channels WHERE id = ? OR url = ?", (url_or_id, url_or_id)).fetchone()

    def channel_dir_names(self) -> set[str]:
        return {row[0] for row in self.conn.execute("SELECT dir_name FROM channels")}

    def set_channel_build(self, channel_id: str, fingerprint: str, md_path: str, now: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE channels SET fingerprint = ?, md_path = ?, built_at = ? WHERE id = ?",
                (fingerprint, md_path, now, channel_id),
            )

    # ----------------------------------------------------------------- playlists
    def upsert_playlist(
        self,
        channel_id: str,
        playlist_id: str,
        title: str,
        url: str,
        position: int,
        dir_name: str,
        is_virtual: bool,
        now: str,
        manual: bool = False,
    ) -> None:
        """Insert or refresh a playlist; dir_name, build state and the manual flag are kept on update."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO playlists (id, channel_id, title, url, position, dir_name, is_virtual, manual, active, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    position = excluded.position,
                    manual = MAX(playlists.manual, excluded.manual),
                    active = 1,
                    last_seen = excluded.last_seen
                """,
                (playlist_id, channel_id, title, url, position, dir_name, int(is_virtual), int(manual), now, now),
            )

    def get_playlist(self, playlist_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()

    def playlist_dir_name(self, playlist_id: str) -> str | None:
        row = self.conn.execute("SELECT dir_name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        return row[0] if row else None

    def playlist_dir_names(self, channel_id: str) -> set[str]:
        rows = self.conn.execute("SELECT dir_name FROM playlists WHERE channel_id = ?", (channel_id,))
        return {row[0] for row in rows}

    def playlist_titles(self, channel_id: str) -> dict[str, str]:
        rows = self.conn.execute("SELECT id, title FROM playlists WHERE channel_id = ?", (channel_id,))
        return {row[0]: row[1] for row in rows}

    def active_playlists(self, channel_id: str) -> list[sqlite3.Row]:
        """Active playlists in channel order; the virtual 'unsorted' playlist always comes last."""
        return self.conn.execute(
            "SELECT * FROM playlists WHERE channel_id = ? AND active = 1 ORDER BY is_virtual, position",
            (channel_id,),
        ).fetchall()

    def deactivate_missing_playlists(self, channel_id: str, keep_ids: Iterable[str]) -> int:
        keep = list(keep_ids)
        marks = ",".join("?" * len(keep)) if keep else "''"
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE playlists SET active = 0 WHERE channel_id = ? AND active = 1 AND is_virtual = 0 AND manual = 0 AND id NOT IN ({marks})",
                (channel_id, *keep),
            )
        return cur.rowcount

    def next_playlist_position(self, channel_id: str) -> int:
        """Position for a playlist added on its own (after all playlists seen so far on the channel)."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM playlists WHERE channel_id = ? AND is_virtual = 0",
            (channel_id,),
        ).fetchone()
        return int(row[0])

    def set_playlist_build(self, playlist_id: str, fingerprint: str, md_path: str, now: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE playlists SET fingerprint = ?, md_path = ?, built_at = ? WHERE id = ?",
                (fingerprint, md_path, now, playlist_id),
            )

    # -------------------------------------------------------------------- videos
    def upsert_video(
        self,
        channel_id: str,
        video: VideoInfo,
        status: str,
        error: str | None,
        now: str,
        manual: bool = False,
    ) -> None:
        """Insert a newly discovered video or refresh metadata of a known one.

        Processing state belongs to the pipeline, so sync only flips skipped <-> pending
        (a stream ended / a private video became public, or the other way round).
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO videos (id, channel_id, title, url, duration, upload_date, status, error, manual, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title       = CASE WHEN excluded.error LIKE 'unavailable%' THEN videos.title ELSE excluded.title END,
                    url         = excluded.url,
                    duration    = COALESCE(excluded.duration, videos.duration),
                    upload_date = COALESCE(excluded.upload_date, videos.upload_date),
                    manual      = MAX(videos.manual, excluded.manual),
                    last_seen   = excluded.last_seen,
                    status = CASE
                        WHEN videos.status = 'skipped' AND excluded.status = 'pending' THEN 'pending'
                        WHEN videos.status IN ('pending', 'error') AND excluded.status = 'skipped' THEN 'skipped'
                        ELSE videos.status END,
                    error = CASE
                        WHEN videos.status = 'skipped' AND excluded.status = 'pending' THEN NULL
                        WHEN videos.status IN ('pending', 'error') AND excluded.status = 'skipped' THEN excluded.error
                        ELSE videos.error END
                """,
                (
                    video.id,
                    channel_id,
                    video.title,
                    video.url,
                    video.duration,
                    video.upload_date,
                    status,
                    error,
                    int(manual),
                    now,
                    now,
                ),
            )

    def manual_video_ids(self, channel_id: str) -> list[str]:
        """Videos added by their own URL (they may be unlisted, i.e. invisible on the channel tabs)."""
        rows = self.conn.execute("SELECT id FROM videos WHERE channel_id = ? AND manual = 1", (channel_id,))
        return [row[0] for row in rows]

    def replace_membership(self, playlist_id: str, items: Iterable[tuple[str, int]]) -> None:
        """Set the exact (video_id, position) list of a playlist."""
        rows = [(playlist_id, video_id, position) for video_id, position in items]
        with self.conn:
            self.conn.execute("DELETE FROM playlist_videos WHERE playlist_id = ?", (playlist_id,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO playlist_videos (playlist_id, video_id, position) VALUES (?, ?, ?)", rows
            )

    def add_membership(self, playlist_id: str, video_id: str) -> None:
        """Append one video to a playlist (no-op if it is already there)."""
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO playlist_videos (playlist_id, video_id, position)
                SELECT ?, ?, COALESCE(MAX(position), -1) + 1 FROM playlist_videos WHERE playlist_id = ?
                """,
                (playlist_id, video_id, playlist_id),
            )

    def remove_membership(self, playlist_id: str, video_ids: Iterable[str]) -> int:
        ids = list(video_ids)
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.conn:
            cur = self.conn.execute(
                f"DELETE FROM playlist_videos WHERE playlist_id = ? AND video_id IN ({marks})", (playlist_id, *ids)
            )
        return cur.rowcount

    def video_playlist_ids(self, video_id: str) -> list[str]:
        """Active playlists (real and virtual) that contain the video, in channel order."""
        rows = self.conn.execute(
            """
            SELECT p.id FROM playlist_videos pv
            JOIN playlists p ON p.id = pv.playlist_id
            WHERE pv.video_id = ? AND p.active = 1
            ORDER BY p.is_virtual, p.position
            """,
            (video_id,),
        )
        return [row[0] for row in rows]

    def playlist_videos(self, playlist_id: str) -> list[sqlite3.Row]:
        """Videos of a playlist (full video rows + their position), in playlist order."""
        return self.conn.execute(
            """
            SELECT v.*, pv.position AS position
            FROM playlist_videos pv
            JOIN videos v ON v.id = pv.video_id
            WHERE pv.playlist_id = ?
            ORDER BY pv.position
            """,
            (playlist_id,),
        ).fetchall()

    def membership_pairs(self, channel_id: str) -> set[tuple[str, str]]:
        """(playlist_id, video_id) for every active playlist of the channel."""
        rows = self.conn.execute(
            """
            SELECT pv.playlist_id, pv.video_id FROM playlist_videos pv
            JOIN playlists p ON p.id = pv.playlist_id
            WHERE p.channel_id = ? AND p.active = 1
            """,
            (channel_id,),
        )
        return {(row[0], row[1]) for row in rows}

    def video_ids_in_real_playlists(self, channel_id: str) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT pv.video_id
            FROM playlist_videos pv
            JOIN playlists p ON p.id = pv.playlist_id
            WHERE p.channel_id = ? AND p.is_virtual = 0 AND p.active = 1
            """,
            (channel_id,),
        )
        return {row[0] for row in rows}

    def get_video(self, video_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()

    def bump_attempts(self, video_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE videos SET attempts = attempts + 1 WHERE id = ?", (video_id,))

    def set_audio(self, video_id: str, audio_path: str | None) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE videos
                SET audio_path = ?,
                    status = CASE WHEN status IN ('pending', 'error') THEN 'downloaded' ELSE status END
                WHERE id = ?
                """,
                (audio_path, video_id),
            )

    def mark_done(self, video_id: str, transcript_path: str, home_playlist_id: str, now: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE videos
                SET status = 'done', transcript_path = ?, home_playlist_id = ?, transcribed_at = ?, error = NULL
                WHERE id = ?
                """,
                (transcript_path, home_playlist_id, now, video_id),
            )

    def mark_error(self, video_id: str, message: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE videos SET status = 'error', error = ? WHERE id = ?", (message, video_id))

    def reset_video(self, video_id: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE videos
                SET status = 'pending', transcript_path = NULL, home_playlist_id = NULL,
                    transcribed_at = NULL, attempts = 0, error = NULL
                WHERE id = ?
                """,
                (video_id,),
            )

    def reset_errors(self, channel_id: str, video_ids: Iterable[str] | None = None) -> int:
        """Give failed videos a fresh set of attempts — the whole channel, or only the given videos."""
        sql = (
            "UPDATE videos SET status = 'pending', attempts = 0, error = NULL WHERE channel_id = ? AND status = 'error'"
        )
        params: list[str] = [channel_id]
        if video_ids is not None:
            ids = list(video_ids)
            if not ids:
                return 0
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params += ids
        with self.conn:
            cur = self.conn.execute(sql, params)
        return cur.rowcount

    def done_videos(self, channel_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, title, transcript_path FROM videos WHERE channel_id = ? AND status = 'done'", (channel_id,)
        ).fetchall()

    def entity_counts(self, channel_id: str) -> tuple[int, int]:
        """(real playlists, videos) known for the channel — diffed before/after a sync to report what's new."""
        playlists = self.conn.execute(
            "SELECT COUNT(*) FROM playlists WHERE channel_id = ? AND is_virtual = 0", (channel_id,)
        ).fetchone()[0]
        videos = self.conn.execute("SELECT COUNT(*) FROM videos WHERE channel_id = ?", (channel_id,)).fetchone()[0]
        return playlists, videos

    # --------------------------------------------------------------------- stats
    def status_counts(self, channel_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM videos WHERE channel_id = ? GROUP BY status", (channel_id,)
        )
        return {row[0]: row[1] for row in rows}

    def playlist_stats(self, channel_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT p.id, p.title, p.is_virtual, p.built_at,
                   COUNT(pv.video_id)                    AS total,
                   COALESCE(SUM(v.status = 'done'), 0)    AS done,
                   COALESCE(SUM(v.status = 'error'), 0)   AS errors,
                   COALESCE(SUM(v.status = 'skipped'), 0) AS skipped
            FROM playlists p
            LEFT JOIN playlist_videos pv ON pv.playlist_id = p.id
            LEFT JOIN videos v ON v.id = pv.video_id
            WHERE p.channel_id = ? AND p.active = 1
            GROUP BY p.id
            ORDER BY p.is_virtual, p.position
            """,
            (channel_id,),
        ).fetchall()

    def errored_videos(self, channel_id: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, title, error, attempts FROM videos
            WHERE channel_id = ? AND status = 'error'
            ORDER BY last_seen DESC LIMIT ?
            """,
            (channel_id, limit),
        ).fetchall()
