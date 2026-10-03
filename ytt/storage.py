"""Filesystem layout for one channel and cross-platform safe file names."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .config import Config

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(name: str, max_len: int = 60) -> str:
    """Turn an arbitrary title into a safe file/folder name; Unicode letters (Cyrillic etc.) are kept.

    Guarantees: non-empty, at most `max_len` characters (values below 8 are treated as 8), no characters
    forbidden on Windows/macOS, no leading/trailing spaces or dots, not a Windows device name
    (CON, PRN, COM1 ... also with an "extension", e.g. "con.1"), and idempotent.
    """
    max_len = max(max_len, 8)
    name = unicodedata.normalize("NFC", name or "")
    name = _FORBIDDEN.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        cut = name[:max_len]
        space = cut.rfind(" ")
        if space > max_len // 2:  # prefer cutting at a word boundary
            cut = cut[:space]
        name = cut.rstrip(" .")
    if not name:
        name = "untitled"
    stem, dot, rest = name.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        name = f"{stem}_{dot}{rest}"
    return name


class Layout:
    """Where things live for one channel::

    <data_dir>/<Channel>/audio/<video_id>.mp3
    <data_dir>/<Channel>/transcripts/<Playlist>/<NNN - Title [video_id]>.txt
    <data_dir>/<Channel>/output/<Playlist>.md
    <data_dir>/<Channel>/output/_ALL.md
    """

    CHANNEL_FILE = "_ALL.md"

    def __init__(self, cfg: Config, channel_dir_name: str) -> None:
        self.root = Path(cfg.paths.data_dir) / channel_dir_name
        self.audio_dir = self.root / cfg.paths.audio_subdir
        self.transcripts_dir = self.root / cfg.paths.transcripts_subdir
        self.output_dir = self.root / cfg.paths.output_subdir
        self.max_name_len = cfg.output.max_name_len

    def transcript_path(self, playlist_dir: str, position: int, title: str, video_id: str) -> Path:
        file_name = f"{position:03d} - {safe_name(title, self.max_name_len)} [{video_id}].txt"
        return self.transcripts_dir / playlist_dir / file_name

    def playlist_md_path(self, playlist_dir: str) -> Path:
        return self.output_dir / f"{playlist_dir}.md"

    def channel_md_path(self) -> Path:
        return self.output_dir / self.CHANNEL_FILE
