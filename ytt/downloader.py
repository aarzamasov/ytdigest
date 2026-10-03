"""Download a video's audio with a user-configurable shell command (yt-dlp by default)."""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import DownloadConfig

log = logging.getLogger(__name__)

# Any of these is accepted as "audio is ready" (lets you switch the command to m4a/opus if you like).
AUDIO_EXTS = ("mp3", "m4a", "opus", "webm", "ogg", "wav", "flac", "aac", "mka", "mp4")


class DownloadError(RuntimeError):
    pass


class _SafeDict(dict):
    """format_map helper: unknown {placeholders} are left as-is instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Downloader:
    def __init__(self, cfg: DownloadConfig) -> None:
        self.cfg = cfg

    def check(self) -> None:
        """Fail fast if the download executable is missing (before burning per-video attempts)."""
        executable = shlex.split(self.cfg.command)[0]
        if shutil.which(executable) is None:
            raise DownloadError(
                f"'{executable}' not found in PATH — install it (pip install -U yt-dlp / brew install yt-dlp)"
            )

    def find_existing(self, audio_dir: Path, video_id: str) -> Path | None:
        for ext in AUDIO_EXTS:
            candidate = audio_dir / f"{video_id}.{ext}"
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

    def download(self, url: str, video_id: str, audio_dir: Path) -> Path:
        """Return the path of the audio file for `video_id`, downloading it if needed."""
        audio_dir.mkdir(parents=True, exist_ok=True)
        existing = self.find_existing(audio_dir, video_id)
        if existing is not None:
            log.debug("Audio already present: %s", existing)
            return existing

        # Forward slashes keep shlex happy on Windows too; yt-dlp accepts them.
        output_template = (audio_dir / f"{video_id}.%(ext)s").as_posix()
        command = self.cfg.command.format_map(
            _SafeDict(url=url, video_id=video_id, output_dir=audio_dir.as_posix(), output_template=output_template)
        )
        args = shlex.split(command)
        log.debug("Running: %s", " ".join(args))
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.cfg.timeout_sec,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DownloadError(f"command not found: {args[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DownloadError(f"download timed out after {self.cfg.timeout_sec}s") from exc

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
            raise DownloadError(f"exit code {proc.returncode}: {' | '.join(tail) or 'no output'}")

        found = self.find_existing(audio_dir, video_id)
        if found is None:
            raise DownloadError("command finished but no audio file was produced (check -o template)")
        return found
