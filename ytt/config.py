"""Configuration: dataclasses with sane defaults, optionally overridden from a TOML file."""

from __future__ import annotations

import dataclasses
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = REPO_ROOT / "config.toml"


@dataclass
class PathsConfig:
    data_dir: str = "data"  # root for db, log and per-channel folders
    db_file: str = "ytt.sqlite3"  # inside data_dir
    log_file: str = "ytt.log"  # inside data_dir
    audio_subdir: str = "audio"  # data_dir/<Channel>/audio/<video_id>.mp3
    transcripts_subdir: str = "transcripts"  # data_dir/<Channel>/transcripts/<Playlist>/<NNN - Title [id]>.txt
    output_subdir: str = "output"  # data_dir/<Channel>/output/<Playlist>.md + _ALL.md


@dataclass
class YouTubeConfig:
    channel_tabs: list[str] = field(default_factory=lambda: ["videos"])  # also "shorts", "streams"
    cookies_from_browser: str = ""  # "chrome" | "firefox" | "safari" | ... when YouTube asks to sign in
    unsorted_playlist_title: str = "Без плейлиста"  # virtual playlist for videos outside any playlist
    skip_live: bool = True  # skip ongoing/upcoming streams; they are picked up later as VODs


@dataclass
class DownloadConfig:
    # Placeholders: {url} {video_id} {output_dir} {output_template}
    command: str = (
        "yt-dlp --no-playlist --no-overwrites --retries 5 "
        "-f bestaudio/best -x --audio-format mp3 --audio-quality 5 "
        '-o "{output_template}" "{url}"'
    )
    timeout_sec: int = 1800
    keep_audio: bool = True  # False -> delete audio right after a successful transcription
    max_attempts: int = 3  # per video; `--retry-errors` resets the counter


@dataclass
class TranscribeConfig:
    backend: str = "faster_whisper"  # "faster_whisper" (CPU / NVIDIA) | "mlx_whisper" (Apple Silicon)
    model: str = "large-v3-turbo"  # tiny | base | small | medium | large-v3 | large-v3-turbo | HF repo
    language: str | None = "ru"  # None -> auto-detect per video
    device: str = "auto"  # faster_whisper: auto | cpu | cuda
    compute_type: str = "default"  # faster_whisper: default | int8 (CPU) | float16 (GPU)
    beam_size: int = 5
    vad_filter: bool = True  # drop silence before decoding (faster, fewer hallucinations)
    condition_on_previous_text: bool = False  # False avoids repetition loops on long recordings
    chunk_sec: float = (
        10.0  # a new time-coded block starts at the first segment boundary after this many seconds (0 = off)
    )
    paragraph_gap_sec: float = 2.0  # pause longer than this also starts a new block
    paragraph_max_chars: int = 900  # soft limit per block (breaks at sentence end)
    timestamps: bool = True  # every block starts with [HH:MM:SS]; the digests turn these into YouTube links


@dataclass
class OutputConfig:
    timestamp_links: bool = True  # [HH:MM:SS] in the digests becomes a link that opens the video at that second
    dedupe_in_playlist_files: bool = False  # True -> video already transcribed under another playlist gets a reference
    max_name_len: int = 60  # file/folder names built from titles are cut to this length


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Load config.toml (explicit path, or <repo>/config.toml if it exists); fall back to defaults."""
        base_dir = REPO_ROOT
        raw: dict[str, Any] = {}
        if path is not None or DEFAULT_CONFIG_FILE.exists():
            config_path = Path(path) if path is not None else DEFAULT_CONFIG_FILE
            with config_path.open("rb") as fh:
                raw = tomllib.load(fh)
            base_dir = config_path.resolve().parent
            log.debug("Loaded config from %s", config_path)

        cfg = cls(
            paths=_section(PathsConfig, raw, "paths"),
            youtube=_section(YouTubeConfig, raw, "youtube"),
            download=_section(DownloadConfig, raw, "download"),
            transcribe=_section(TranscribeConfig, raw, "transcribe"),
            output=_section(OutputConfig, raw, "output"),
        )

        # Relative data_dir is resolved against the config file location (stable for cron jobs).
        data_dir = Path(cfg.paths.data_dir)
        if not data_dir.is_absolute():
            cfg.paths.data_dir = str((base_dir / data_dir).resolve())

        lang = (cfg.transcribe.language or "").strip().lower()
        cfg.transcribe.language = None if lang in ("", "auto") else cfg.transcribe.language
        return cfg


def _section(cls: type, raw: dict[str, Any], name: str) -> Any:
    data = raw.get(name) or {}
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        log.warning("config [%s]: ignoring unknown keys %s", name, unknown)
    return cls(**{k: v for k, v in data.items() if k in known})
