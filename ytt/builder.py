"""Markdown digests (one per playlist, one for the whole channel) and change fingerprints."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from .util import fmt_duration, sha256_text

Row = Any  # sqlite3.Row or dict — anything indexable by column name
ReadText = Callable[[Row], str | None]

# Labels used inside the generated documents (the documents are for humans/Gemini, not for code).
LABELS = {
    "channel": "Канал",
    "playlist": "Плейлист",
    "playlists": "Плейлистов",
    "videos": "Видео",
    "built": "Собрано",
    "contents": "Содержание",
    "unavailable": "видео недоступно",
    "no_transcript": "транскрипт пока не готов",
    "see_above": "текст уже приведён выше, в плейлисте",
    "see_playlist": "текст приведён в файле плейлиста",
    "partial": "Канал целиком ещё не синхронизировался — здесь только плейлисты и видео, добавленные по отдельности.",
}


# ------------------------------------------------------------------ fingerprints
def playlist_fingerprint(playlist: Row, videos: Sequence[Row], render_key: str = "") -> str:
    """Changes when the playlist title, membership/order, any video's transcript state, or the
    rendering options (`render_key`, e.g. timestamp links on/off) change."""
    parts = [str(playlist["id"]), str(playlist["title"]), render_key]
    for v in videos:
        parts.append(f"{v['id']}|{v['position']}|{v['status']}|{v['transcribed_at'] or ''}|{v['title']}")
    return sha256_text("\n".join(parts))


def channel_fingerprint(channel: Row, bundles: Sequence[tuple[Row, Sequence[Row]]], render_key: str = "") -> str:
    """Also flips when the channel goes from "assembled from single playlists/videos" to fully synced."""
    coverage = "full" if is_fully_synced(channel) else "partial"
    parts = [str(channel["name"]), coverage, render_key] + [
        playlist_fingerprint(pl, vids, render_key) for pl, vids in bundles
    ]
    return sha256_text("\n".join(parts))


def is_fully_synced(channel: Row) -> bool:
    """False while the channel is only known through playlists/videos added one by one."""
    return bool(channel["last_synced"])


# --------------------------------------------------------------------- rendering
def render_playlist_md(
    channel: Row,
    playlist: Row,
    videos: Sequence[Row],
    read_text: ReadText,
    built_at: str,
    playlist_titles: Mapping[str, str] | None = None,
    dedupe_foreign: bool = False,
    membership: Collection[tuple[str, str]] | None = None,
    timestamp_links: bool = False,
) -> str:
    """One playlist. With dedupe_foreign, a video whose text lives under another playlist gets a
    one-line reference — but only while that playlist still lists it (membership check).
    With timestamp_links, [HH:MM:SS] block prefixes become links into the video."""
    L = LABELS
    out = [
        f"# {playlist['title']}",
        "",
        f"{L['channel']}: {channel['name']} — {channel['url']}  ",
        f"{L['playlist']}: {playlist['url']}  ",
        f"{L['videos']}: {len(videos)}  ",
        f"{L['built']}: {built_at}",
        "",
    ]
    titles = playlist_titles or {}
    for i, v in enumerate(videos, 1):
        out += [f"## {i}. {v['title']}", "", _meta_line(v), ""]
        home = v["home_playlist_id"]
        home_lists_it = membership is None or (str(home), str(v["id"])) in membership
        if dedupe_foreign and home and home != playlist["id"] and home in titles and home_lists_it:
            out.append(f"_({L['see_playlist']} «{titles[home]}»)_")
        else:
            out.append(_body(v, read_text, timestamp_links))
        out += ["", "---", ""]
    return "\n".join(out).rstrip() + "\n"


def render_channel_md(
    channel: Row,
    bundles: Sequence[tuple[Row, Sequence[Row]]],
    read_text: ReadText,
    built_at: str,
    timestamp_links: bool = False,
) -> str:
    """One document for the whole channel; a video that sits in several playlists gets its text once."""
    L = LABELS
    unique_videos = {str(v["id"]) for _, vids in bundles for v in vids}
    out = [
        f"# {channel['name']}",
        "",
        f"{L['channel']}: {channel['url']}  ",
        f"{L['playlists']}: {len(bundles)} · {L['videos']}: {len(unique_videos)}  ",
        f"{L['built']}: {built_at}",
        "",
    ]
    if not is_fully_synced(channel):
        out += [f"_{L['partial']}_", ""]
    out += [f"## {L['contents']}", ""]
    for k, (pl, vids) in enumerate(bundles, 1):
        out.append(f"{k}. {pl['title']} — {len(vids)}")
    out.append("")

    placed: dict[str, str] = {}  # video id -> title of the playlist where its text was placed
    for k, (pl, vids) in enumerate(bundles, 1):
        out += [f"## {k}. {pl['title']}", "", f"{L['playlist']}: {pl['url']} · {L['videos']}: {len(vids)}", ""]
        for i, v in enumerate(vids, 1):
            out += [f"### {k}.{i}. {v['title']}", "", _meta_line(v), ""]
            vid = str(v["id"])
            if vid in placed:
                out.append(f"_({L['see_above']} «{placed[vid]}»)_")
            else:
                out.append(_body(v, read_text, timestamp_links))
                placed[vid] = str(pl["title"])
            out += ["", "---", ""]
    return "\n".join(out).rstrip() + "\n"


# ----------------------------------------------------------------------- helpers
def _meta_line(v: Row) -> str:
    bits = [str(v["url"])]
    if v["duration"]:
        bits.append(fmt_duration(v["duration"]))
    if v["upload_date"]:
        bits.append(str(v["upload_date"]))
    return " · ".join(bits)


_TIMESTAMP_PREFIX = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] ", re.MULTILINE)


def linkify_timestamps(text: str, video_id: str) -> str:
    """`[00:12:35] text` at the start of a block -> `[00:12:35](https://youtu.be/<id>?t=755) text`.

    Markdown viewers (Obsidian, GitHub, VS Code) render it as a clickable time code that opens the
    video at that second; the plain .txt transcript keeps the bare [HH:MM:SS] form.
    """

    def link(match: re.Match[str]) -> str:
        h, m, sec = (int(g) for g in match.groups())
        return f"[{match.group(1)}:{match.group(2)}:{match.group(3)}](https://youtu.be/{video_id}?t={h * 3600 + m * 60 + sec}) "

    return _TIMESTAMP_PREFIX.sub(link, text)


def _body(v: Row, read_text: ReadText, timestamp_links: bool = False) -> str:
    if v["status"] == "done":
        text = read_text(v)
        if text and text.strip():
            text = text.strip()
            return linkify_timestamps(text, str(v["id"])) if timestamp_links else text
    if v["status"] == "skipped":
        reason = f": {v['error']}" if v["error"] else ""
        return f"_({LABELS['unavailable']}{reason})_"
    return f"_({LABELS['no_transcript']}: {v['status']})_"
