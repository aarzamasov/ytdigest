"""Speech-to-text backends (faster-whisper, mlx-whisper) and paragraph assembly."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import TranscribeConfig
from .util import fmt_ts

log = logging.getLogger(__name__)

_PROGRESS_EVERY_SEC = 600.0  # log progress every 10 minutes of audio


@dataclass
class Segment:
    start: float
    end: float
    text: str


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> list[Segment]: ...


class FasterWhisperTranscriber:
    """CTranslate2-based Whisper: works on CPU everywhere and on NVIDIA GPUs (device='cuda')."""

    def __init__(self, cfg: TranscribeConfig) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("faster-whisper is not installed: pip install faster-whisper") from exc
        self.cfg = cfg
        self.model = WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type)

    def transcribe(self, audio_path: Path) -> list[Segment]:
        segments, info = self.model.transcribe(
            str(audio_path),
            language=self.cfg.language,
            beam_size=self.cfg.beam_size,
            vad_filter=self.cfg.vad_filter,
            condition_on_previous_text=self.cfg.condition_on_previous_text,
        )
        total = float(info.duration or 0)
        log.debug("Language %s (p=%.2f), audio %.0fs", info.language, info.language_probability, total)
        out: list[Segment] = []
        next_report = _PROGRESS_EVERY_SEC
        for seg in segments:  # generator: decoding happens while iterating
            out.append(Segment(float(seg.start), float(seg.end), seg.text))
            if total and seg.end >= next_report:
                log.info("    ... %s / %s", fmt_ts(seg.end), fmt_ts(total))
                next_report += _PROGRESS_EVERY_SEC
        return out


_MLX_REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


class MlxWhisperTranscriber:
    """Apple Silicon (M1+) backend; several times faster than CPU faster-whisper on a Mac."""

    def __init__(self, cfg: TranscribeConfig) -> None:
        try:
            import mlx_whisper
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("mlx-whisper is not installed (Apple Silicon only): pip install mlx-whisper") from exc
        self.cfg = cfg
        self.mlx_whisper = mlx_whisper
        self.repo = cfg.model if "/" in cfg.model else _MLX_REPOS.get(cfg.model, f"mlx-community/whisper-{cfg.model}")

    def transcribe(self, audio_path: Path) -> list[Segment]:
        result = self.mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=self.repo,
            language=self.cfg.language,
            condition_on_previous_text=self.cfg.condition_on_previous_text,
            verbose=None,
        )
        return [Segment(float(s["start"]), float(s["end"]), str(s["text"])) for s in result.get("segments", [])]


def get_transcriber(cfg: TranscribeConfig) -> Transcriber:
    backend = cfg.backend.lower().replace("-", "_")
    if backend == "faster_whisper":
        return FasterWhisperTranscriber(cfg)
    if backend == "mlx_whisper":
        return MlxWhisperTranscriber(cfg)
    raise ValueError(f"Unknown transcribe.backend '{cfg.backend}' (use faster_whisper or mlx_whisper)")


def segments_to_text(
    segments: Iterable[Segment],
    gap_sec: float = 2.0,
    max_chars: int = 900,
    timestamps: bool = False,
    chunk_sec: float = 0.0,
) -> str:
    """Join Whisper segments into blocks separated by blank lines.

    A new block starts at the first segment boundary once the block spans `chunk_sec` seconds
    (0 disables this), after a pause longer than `gap_sec`, or when the block exceeds `max_chars`
    and the last sentence has ended (hard cut at 2x max_chars). With `timestamps`, every block is
    prefixed with [HH:MM:SS] of its first segment — the digests turn that into a YouTube link.
    """
    paragraphs: list[tuple[float, str]] = []
    buf: list[str] = []
    buf_len = 0
    buf_start = 0.0
    prev_end: float | None = None

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            paragraphs.append((buf_start, " ".join(buf)))
        buf, buf_len = [], 0

    for seg in segments:
        text = " ".join(seg.text.split())
        if not text:
            continue
        if buf:
            gap = seg.start - prev_end if prev_end is not None else 0.0
            sentence_ended = buf[-1][-1] in '.!?…»")'
            span_reached = chunk_sec > 0 and seg.start - buf_start >= chunk_sec
            if span_reached or gap >= gap_sec or (buf_len >= max_chars and sentence_ended) or buf_len >= 2 * max_chars:
                flush()
        if not buf:
            buf_start = seg.start
        buf.append(text)
        buf_len += len(text) + 1
        prev_end = seg.end
    flush()

    if timestamps:
        return "\n\n".join(f"[{fmt_ts(start)}] {text}" for start, text in paragraphs)
    return "\n\n".join(text for _, text in paragraphs)
