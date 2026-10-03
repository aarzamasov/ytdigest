"""Discovery via yt-dlp in library mode (metadata only, no downloads).

Three kinds of input are understood: a channel, a single playlist and a single video.
`detect_target` tells them apart from a URL / id; the `YouTube` class resolves each
kind to channel, playlist and video metadata.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import YouTubeConfig

log = logging.getLogger(__name__)

YOUTUBE_BASE = "https://www.youtube.com"
_TAB_RE = re.compile(
    r"/(videos|playlists|shorts|streams|live|featured|about|community|podcasts|releases|courses|search)/?$",
    re.IGNORECASE,
)
_LIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
# Bare playlist ids: user playlists (PL...), album playlists (OL...) and channel uploads (UU...).
_PLAYLIST_ID_RE = re.compile(r"^(?:(?:PL|OL)[A-Za-z0-9_-]{13,}|UU[A-Za-z0-9_-]{22})$")
_VIDEO_PATH_RE = re.compile(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})(?:[/?#]|$)")
_UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}
LIVE_STATUSES = {"is_live", "is_upcoming", "post_live"}


class YouTubeError(RuntimeError):
    """Raised when yt-dlp cannot return usable metadata."""


@dataclass(frozen=True)
class ChannelInfo:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class PlaylistInfo:
    id: str
    title: str
    url: str


@dataclass(frozen=True)
class VideoInfo:
    id: str
    title: str
    url: str
    duration: int | None
    upload_date: str | None  # YYYY-MM-DD (approximate for channel-tab entries)
    available: bool  # False for private/deleted videos
    live_status: str | None  # yt-dlp live_status: "is_live", "was_live", None, ...

    @property
    def is_live(self) -> bool:
        return self.live_status in LIVE_STATUSES


@dataclass(frozen=True)
class PlaylistDetails:
    """A playlist page: the playlist itself, its owner channel (None for mixes etc.) and its videos."""

    playlist: PlaylistInfo
    channel: ChannelInfo | None
    videos: list[VideoInfo]


@dataclass(frozen=True)
class VideoDetails:
    video: VideoInfo
    channel: ChannelInfo


class Kind:
    CHANNEL = "channel"
    PLAYLIST = "playlist"
    VIDEO = "video"


@dataclass(frozen=True)
class Target:
    """What the user pointed at.

    `kind` says what to work on; the id fields hold whatever could be read from the input, so a
    channel-kind target may carry only a video id (``--channel`` on a video URL means "the channel
    this video belongs to").
    """

    kind: str
    channel: str | None = None  # channel URL, @handle or UC... id, as given
    playlist_id: str | None = None
    video_id: str | None = None
    note: str | None = None  # something worth telling the user about the detection

    @property
    def label(self) -> str:
        if self.kind == Kind.VIDEO:
            return f"video {self.video_id}"
        if self.kind == Kind.PLAYLIST:
            return f"playlist {self.playlist_id}"
        return f"channel {self.channel or self.playlist_id or self.video_id}"


def detect_target(raw: str, kind: str | None = None) -> Target:
    """Work out whether `raw` is a channel, a playlist or a video; `kind` forces the answer.

    Auto rules: a video id in the URL wins (watch?v=, youtu.be/, /shorts/, /live/, /embed/),
    then a `list=` playlist id (an uploads playlist UU... is treated as its channel),
    otherwise it is a channel (URL, @handle, UC... id or a bare handle).
    """
    text = raw.strip()
    if not text:
        raise ValueError("empty target")
    if kind not in (None, Kind.CHANNEL, Kind.PLAYLIST, Kind.VIDEO):
        raise ValueError(f"unknown kind '{kind}'")

    channel: str | None = None
    playlist_id: str | None = None
    video_id: str | None = None

    url = _split_youtube_url(text)
    if url is not None:
        host, path, query = url
        if host == "youtu.be":
            first = path.strip("/").split("/", 1)[0]
            video_id = first if _VIDEO_ID_RE.match(first) else None
        else:
            match = _VIDEO_PATH_RE.match(path)
            params = parse_qs(query)
            video_id = match.group(1) if match else _param(params, "v", _VIDEO_ID_RE)
            playlist_id = _param(params, "list", re.compile(r"^[A-Za-z0-9_-]{2,}$"))
            if video_id is None and playlist_id is None:
                channel = text  # /@handle, /channel/UC..., /c/Name, /user/Name (tabs are stripped later)
    elif _PLAYLIST_ID_RE.match(text):
        playlist_id = text  # with --channel this means "the channel that owns the playlist"
    elif kind == Kind.VIDEO and _VIDEO_ID_RE.match(text):
        video_id = text
    else:
        channel = text  # @handle, UC... id or a bare handle

    if playlist_id and playlist_id.startswith("UU") and len(playlist_id) == 24:
        # The auto-generated "uploads" playlist is the channel itself.
        channel, playlist_id = "UC" + playlist_id[2:], None

    note = None
    if kind is None:
        if video_id:
            kind = Kind.VIDEO
            if playlist_id:
                note = "the URL points at a video inside a playlist — working with the video (use --playlist for the whole playlist)"
        elif playlist_id:
            kind = Kind.PLAYLIST
        else:
            kind = Kind.CHANNEL

    if kind == Kind.VIDEO and not video_id:
        raise ValueError(f"no video id in '{raw}' (expected watch?v=..., youtu.be/..., /shorts/... or an 11-char id)")
    if kind == Kind.PLAYLIST and not playlist_id:
        raise ValueError(f"no playlist id in '{raw}' (expected ...playlist?list=PL... or a PL... id)")
    if kind == Kind.CHANNEL and not (channel or playlist_id or video_id):
        raise ValueError(f"cannot read a channel from '{raw}'")
    return Target(kind=kind, channel=channel, playlist_id=playlist_id, video_id=video_id, note=note)


def _split_youtube_url(text: str) -> tuple[str, str, str] | None:
    """(host, path, query) for YouTube URLs (scheme optional); None when `text` is a handle or an id."""
    if text.startswith("@"):
        return None
    has_scheme = re.match(r"^https?://", text, re.IGNORECASE) is not None
    parts = urlsplit(text if has_scheme else f"https://{text}")
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    if host == "youtu.be" or host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        return host, parts.path, parts.query
    if has_scheme:
        raise ValueError(f"'{text}' is not a YouTube URL")
    return None  # e.g. a bare handle such as doctor.ivanov


def _param(params: dict[str, list[str]], name: str, pattern: re.Pattern[str]) -> str | None:
    values = params.get(name) or []
    return values[0] if values and pattern.match(values[0]) else None


def normalize_channel_url(url: str) -> str:
    """Canonical channel URL: https://www.youtube.com/@handle (no tab suffix, query or trailing slash)."""
    url = url.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        if "youtube.com" in url:
            url = "https://" + url.lstrip("/")
        elif url.startswith("UC") and len(url) == 24:
            url = f"{YOUTUBE_BASE}/channel/{url}"
        else:
            url = f"{YOUTUBE_BASE}/{url if url.startswith('@') else '@' + url}"
    url = re.sub(r"^https?://[^/]+", lambda m: m.group(0).lower(), url, flags=re.IGNORECASE)  # scheme/host casing
    url = url.replace("http://", "https://")
    url = re.sub(r"https://(m\.|www\.)?youtube\.com", YOUTUBE_BASE, url)
    return _TAB_RE.sub("", url).rstrip("/")


class _YdlLogger:
    """Route yt-dlp chatter into our logging at DEBUG; keep real errors visible."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


class YouTube:
    def __init__(self, cfg: YouTubeConfig) -> None:
        self.cfg = cfg
        self._ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "extract_flat": True,
            "skip_download": True,
            "ignoreerrors": True,
            "logger": _YdlLogger(),
            # Approximate upload dates for flat channel-tab entries ("3 years ago" -> date).
            "extractor_args": {"youtubetab": {"approximate_date": ["true"]}},
        }
        if cfg.cookies_from_browser:
            self._ydl_opts["cookiesfrombrowser"] = (cfg.cookies_from_browser,)

    # ----------------------------------------------------------------- public API
    def resolve_channel(self, url_or_id: str) -> ChannelInfo:
        """Accepts a channel URL, @handle, UC... id or even a video URL; returns id, name and canonical URL."""
        base = normalize_channel_url(url_or_id)
        problems: list[str] = []
        for tab in ("videos", "playlists", "featured", ""):
            candidate = f"{base}/{tab}" if tab else base
            try:
                info = self._extract(candidate)
            except YouTubeError as exc:
                problems.append(str(exc))
                continue
            channel = _channel_from_info(info, fallback_url=base, allow_own_id=True)
            if channel is not None:
                return channel
        detail = "; ".join(problems) if problems else "no channel_id in metadata"
        raise YouTubeError(f"Could not resolve channel from '{url_or_id}': {detail}")

    def resolve_playlist(self, playlist_id: str) -> PlaylistDetails:
        """One playlist page: title, owner channel and videos (owner is None for mixes / Watch later)."""
        url = f"{YOUTUBE_BASE}/playlist?list={playlist_id}"
        info = self._extract(url)
        pid = info.get("id") if isinstance(info.get("id"), str) else playlist_id
        title = str(info.get("title") or pid).strip()
        playlist = PlaylistInfo(id=str(pid), title=title, url=f"{YOUTUBE_BASE}/playlist?list={pid}")
        return PlaylistDetails(playlist=playlist, channel=_channel_from_info(info), videos=_videos_from_info(info))

    def resolve_video(self, video_id: str) -> VideoDetails:
        """Metadata of one video plus the channel it belongs to (full extraction, no download)."""
        info = self._extract(f"{YOUTUBE_BASE}/watch?v={video_id}", flat=False)
        video = _video_from_entry(info)
        channel = _channel_from_info(info)
        if video is None or channel is None:
            raise YouTubeError(f"Could not read video/channel metadata for {video_id}")
        return VideoDetails(video=video, channel=channel)

    def list_playlists(self, channel_url: str) -> list[PlaylistInfo]:
        """Playlists in the order YouTube shows them on the channel's Playlists tab."""
        try:
            info = self._extract(f"{channel_url}/playlists")
        except YouTubeError as exc:
            log.warning("Playlists tab not available: %s", exc)
            return []
        out: list[PlaylistInfo] = []
        seen: set[str] = set()
        for entry in _iter_entries(info):
            playlist = _playlist_from_entry(entry)
            if playlist and playlist.id not in seen:
                seen.add(playlist.id)
                out.append(playlist)
        return out

    def list_playlist_videos(self, playlist_url: str) -> list[VideoInfo]:
        return _videos_from_info(self._extract(playlist_url))

    def list_channel_videos(self, channel_url: str) -> list[VideoInfo]:
        """All videos from the configured channel tabs (videos / shorts / streams), deduplicated."""
        out: list[VideoInfo] = []
        seen: set[str] = set()
        for tab in self.cfg.channel_tabs:
            try:
                info = self._extract(f"{channel_url}/{tab}")
            except YouTubeError as exc:
                log.warning("Tab '%s' not available: %s", tab, exc)
                continue
            for video in _videos_from_info(info):
                if video.id not in seen:
                    seen.add(video.id)
                    out.append(video)
        return out

    # ------------------------------------------------------------------ internals
    def _extract(self, url: str, flat: bool = True) -> dict[str, Any]:
        """Flat extraction for channel tabs and playlists; flat=False reads one video's own page."""
        try:
            import yt_dlp  # imported lazily so `--help` works without it
        except ImportError as exc:  # pragma: no cover
            raise YouTubeError("yt-dlp is not installed: pip install -U yt-dlp") from exc

        opts = self._ydl_opts if flat else {**self._ydl_opts, "extract_flat": False, "noplaylist": True}
        log.debug("Extracting %s", url)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # process=False for a single video: metadata only, no format selection (which can fail
                # when YouTube's player challenges cannot be solved — irrelevant for us).
                info = ydl.extract_info(url, download=False, process=flat)
        except Exception as exc:  # noqa: BLE001 — yt-dlp raises many exception types
            raise YouTubeError(f"yt-dlp failed for {url}: {exc}") from exc
        if not info:
            raise YouTubeError(f"yt-dlp returned no metadata for {url}")
        return info


def _channel_from_info(
    info: dict[str, Any], fallback_url: str | None = None, allow_own_id: bool = False
) -> ChannelInfo | None:
    """Owner channel of a channel tab / playlist / video page; None when the metadata has no UC... id."""
    cid = info.get("channel_id") or (info.get("id") if allow_own_id else None)
    if not (isinstance(cid, str) and _CHANNEL_ID_RE.match(cid)):
        return None
    name = (info.get("channel") or info.get("uploader") or _strip_tab_suffix(info.get("title")) or cid).strip()
    url = info.get("uploader_url") or info.get("channel_url") or fallback_url or f"{YOUTUBE_BASE}/channel/{cid}"
    return ChannelInfo(id=cid, name=name, url=normalize_channel_url(str(url)))


def _strip_tab_suffix(title: Any) -> str | None:
    if not isinstance(title, str):
        return None
    return re.sub(r"\s+-\s+(Videos|Playlists|Shorts|Live|Home)$", "", title).strip() or None


def _iter_entries(info: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Flatten yt-dlp entries; channel tabs may nest sections as sub-playlists."""
    for entry in info.get("entries") or []:
        if not entry:
            continue
        if entry.get("_type") == "playlist" and entry.get("entries") is not None:
            yield from _iter_entries(entry)
        else:
            yield entry


def _playlist_from_entry(entry: dict[str, Any]) -> PlaylistInfo | None:
    url = str(entry.get("url") or "")
    match = _LIST_RE.search(url)
    pid = match.group(1) if match else entry.get("id")
    if not isinstance(pid, str) or len(pid) == 11:  # 11 chars = a video id, not a playlist
        return None
    title = (entry.get("title") or pid).strip()
    return PlaylistInfo(id=pid, title=title, url=f"{YOUTUBE_BASE}/playlist?list={pid}")


def _videos_from_info(info: dict[str, Any]) -> list[VideoInfo]:
    out: list[VideoInfo] = []
    seen: set[str] = set()
    for entry in _iter_entries(info):
        video = _video_from_entry(entry)
        if video and video.id not in seen:
            seen.add(video.id)
            out.append(video)
    return out


def _video_from_entry(entry: dict[str, Any]) -> VideoInfo | None:
    vid = entry.get("id")
    if not isinstance(vid, str) or len(vid) != 11:
        return None
    title = (entry.get("title") or "").strip()
    available = title.lower() not in _UNAVAILABLE_TITLES and entry.get("availability") != "private"
    duration = entry.get("duration")
    return VideoInfo(
        id=vid,
        title=title or vid,
        url=f"{YOUTUBE_BASE}/watch?v={vid}",
        duration=int(duration) if duration else None,
        upload_date=_upload_date(entry),
        available=available,
        live_status=entry.get("live_status"),
    )


def _upload_date(entry: dict[str, Any]) -> str | None:
    raw = entry.get("upload_date")
    if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if ts:
        return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d")
    return None
