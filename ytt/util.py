"""Small shared helpers: time formatting, hashing, logging setup."""

from __future__ import annotations

import hashlib
import io
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path


def utcnow_iso() -> str:
    """Current UTC time as a compact ISO-8601 string (second precision)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def fmt_duration(seconds: float | int | None) -> str:
    """3723 -> '1:02:03'; 245 -> '4:05'; None -> '?'."""
    if seconds is None:
        return "?"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def fmt_ts(seconds: float) -> str:
    """Transcript timestamp, always HH:MM:SS."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def setup_logging(log_file: Path | None, verbose: bool = False) -> None:
    """Console at INFO (DEBUG with --verbose) plus a DEBUG file log."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):  # not when stdout is replaced (tests, some IDEs)
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # Cyrillic titles on Windows consoles
            except ValueError:
                pass

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(file_handler)

    for noisy in ("urllib3", "httpx", "httpcore", "huggingface_hub", "filelock", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
