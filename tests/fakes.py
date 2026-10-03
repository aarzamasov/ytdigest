"""Shared test doubles: an in-memory YouTube, a downloader that writes stub files, a canned
transcriber, and a Harness that wires them into a Pipeline inside a temporary data directory.

Also `run_tests(namespace)`, so every test module can be executed directly without pytest.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ytt.config import Config  # noqa: E402
from ytt.db import Database  # noqa: E402
from ytt.pipeline import Pipeline, unsorted_playlist_id  # noqa: E402
from ytt.transcriber import Segment  # noqa: E402
from ytt.youtube import (  # noqa: E402
    ChannelInfo,
    Kind,
    PlaylistDetails,
    PlaylistInfo,
    VideoDetails,
    VideoInfo,
    detect_target,
)

logging.disable(logging.CRITICAL)  # keep test output readable; comment out to see pipeline logs

TITLES = {
    1: "Гипертония: что делать",
    2: "Холестерин <мифы> и правда",
    3: "Соль и давление",
    4: "Видео без плейлиста",
    5: "Прямой эфир",
    6: "Скрытое (unlisted) видео",
    7: "Новое видео про аритмию",
    8: "Видео нового плейлиста",
    9: "[Private video]",
}
PRIVATE, LIVE = {9}, {5}
CHANNEL = ChannelInfo("UC" + "x" * 22, "Доктор: Тест/Канал?", "https://www.youtube.com/@doctest")
CHANNEL_DIR = "Доктор Тест Канал"  # safe_name(CHANNEL.name)


def vid(i: int) -> str:
    return f"vid{i:08d}"


def video(i: int) -> VideoInfo:
    return VideoInfo(
        id=vid(i),
        title=TITLES[i],
        url=f"https://www.youtube.com/watch?v={vid(i)}",
        duration=600 + i,
        upload_date=f"2025-01-0{i % 9 + 1}",
        available=i not in PRIVATE,
        live_status="is_live" if i in LIVE else None,
    )


def playlist_info(pid: str, title: str) -> PlaylistInfo:
    return PlaylistInfo(pid, title, f"https://www.youtube.com/playlist?list={pid}")


class FakeYouTube:
    """Stands in for ytt.youtube.YouTube and serves one small channel that tests can mutate."""

    def __init__(self, *_args, **_kwargs) -> None:  # accepts the YouTubeConfig the Pipeline passes
        self.channel = CHANNEL
        # playlists shown on the channel's Playlists tab: id -> (title, video numbers)
        self.playlists: dict[str, tuple[str, list[int]]] = {
            "PLaaa": ("Сердце и сосуды", [1, 2, 9]),
            "PLbbb": ("Питание", [3, 2]),
        }
        # playlists that exist (reachable by URL) but are not on the tab, e.g. unlisted ones
        self.hidden_playlists: dict[str, tuple[str, list[int]]] = {"PLccc": ("Новый плейлист", [8, 4])}
        self.tab_videos: list[int] = [1, 2, 3, 4, 5]  # the Videos tab (video 6 is unlisted)
        self.calls: list[tuple[str, str]] = []

    def _videos(self, pid: str) -> list[VideoInfo]:
        _title, numbers = {**self.playlists, **self.hidden_playlists}[pid]
        return [video(i) for i in numbers]

    def resolve_channel(self, url_or_id: str) -> ChannelInfo:
        self.calls.append(("resolve_channel", url_or_id))
        return self.channel

    def list_playlists(self, channel_url: str) -> list[PlaylistInfo]:
        return [playlist_info(pid, title) for pid, (title, _) in self.playlists.items()]

    def list_playlist_videos(self, playlist_url: str) -> list[VideoInfo]:
        return self._videos(playlist_url.rsplit("=", 1)[1])

    def list_channel_videos(self, channel_url: str) -> list[VideoInfo]:
        return [video(i) for i in self.tab_videos]

    def resolve_playlist(self, playlist_id: str) -> PlaylistDetails:
        self.calls.append(("resolve_playlist", playlist_id))
        if playlist_id.startswith("RD"):  # a mix: no owner channel
            return PlaylistDetails(playlist_info(playlist_id, "Mix"), None, [video(1)])
        title = {**self.playlists, **self.hidden_playlists}[playlist_id][0]
        return PlaylistDetails(playlist_info(playlist_id, title), self.channel, self._videos(playlist_id))

    def resolve_video(self, video_id: str) -> VideoDetails:
        self.calls.append(("resolve_video", video_id))
        return VideoDetails(video(int(video_id[3:])), self.channel)


class FakeDownloader:
    """Writes a stub audio file; `fail_ids` raise instead, `interrupt_ids` simulate Ctrl+C."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.downloads = 0
        self.fail_ids: set[str] = set()
        self.interrupt_ids: set[str] = set()

    def check(self) -> None:
        pass

    def download(self, url: str, video_id: str, audio_dir: Path) -> Path:
        if video_id in self.interrupt_ids:
            raise KeyboardInterrupt
        if video_id in self.fail_ids:
            raise RuntimeError(f"simulated download failure for {video_id}")
        audio_dir.mkdir(parents=True, exist_ok=True)
        path = audio_dir / f"{video_id}.mp3"
        if not path.exists():  # like the real Downloader: existing audio is reused
            self.downloads += 1
            path.write_bytes(b"x" * 10)
        return path


class FakeTranscriber:
    """Canned segments; `fail_ids` raise, `empty_ids` return silence."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.fail_ids: set[str] = set()
        self.empty_ids: set[str] = set()
        self.calls = 0

    def transcribe(self, audio_path: Path) -> list[Segment]:
        self.calls += 1
        if audio_path.stem in self.fail_ids:
            raise RuntimeError(f"simulated transcription failure for {audio_path.stem}")
        if audio_path.stem in self.empty_ids:
            return [Segment(0, 1, "   ")]
        return [Segment(0, 3, f" Это транскрипт файла {audio_path.stem}."), Segment(3.2, 6, "Вторая фраза.")]


class Harness:
    """A pipeline wired to the fakes, inside a temporary data directory. Use as a context manager."""

    def __init__(self, **config_overrides) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt-test-"))
        self.cfg = Config()
        self.cfg.paths.data_dir = str(self.tmp)
        for dotted, value in config_overrides.items():  # e.g. download__keep_audio=False
            section, key = dotted.split("__")
            setattr(getattr(self.cfg, section), key, value)
        self.db = Database(self.tmp / self.cfg.paths.db_file)
        self.pipeline = Pipeline(self.cfg, self.db)
        self.yt = self.pipeline.yt = FakeYouTube()
        self.downloader = self.pipeline.downloader = FakeDownloader()
        self.transcriber = self.pipeline._transcriber = FakeTranscriber()
        self.root = self.tmp / CHANNEL_DIR
        self.out = self.root / self.cfg.paths.output_subdir
        self.unsorted = unsorted_playlist_id(CHANNEL.id)

    def __enter__(self) -> Harness:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def run(self, raw: str, kind: str | None = None, **process_kwargs):
        """Same as `python main.py run <raw> [--kind]`."""
        target = detect_target(raw, kind)
        channel = self.pipeline.sync(target)
        self.pipeline.process(
            channel,
            only_playlist=target.playlist_id if target.kind == Kind.PLAYLIST else None,
            only_video=target.video_id if target.kind == Kind.VIDEO else None,
            **process_kwargs,
        )
        return self.db.get_channel(channel["id"]), target

    def channel(self):
        return self.db.get_channel(CHANNEL.id)

    def statuses(self) -> dict[str, str]:
        return {row[0]: row[1] for row in self.db.conn.execute("SELECT id, status FROM videos")}

    def members(self, playlist_id: str) -> list[str]:
        return [row["id"] for row in self.db.playlist_videos(playlist_id)]

    def digests(self) -> dict[str, int]:
        """name -> mtime_ns of every digest; compare snapshots to see what was rebuilt."""
        return {f.name: f.stat().st_mtime_ns for f in self.out.glob("*.md")} if self.out.exists() else {}

    def read(self, name: str) -> str:
        return (self.out / name).read_text(encoding="utf-8")

    def close(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def rebuilt(before: dict[str, int], after: dict[str, int]) -> set[str]:
    return {name for name, mtime in after.items() if before.get(name) != mtime}


def expect_raises(exc_type: type[BaseException], fn, *args, contains: str = "", **kwargs) -> BaseException:
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        assert contains in str(exc), f"{exc_type.__name__} raised, but {contains!r} not in {str(exc)!r}"
        return exc
    raise AssertionError(f"expected {exc_type.__name__} from {getattr(fn, '__name__', fn)}")


def run_tests(namespace: dict) -> None:
    """Run every `test_*` function of a module in definition order; exit 1 on the first failure."""
    tests = [obj for name, obj in namespace.items() if name.startswith("test_") and callable(obj)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"OK    {test.__name__}")
        except Exception:  # noqa: BLE001 — report and keep going
            failed += 1
            print(f"FAIL  {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed in {namespace.get('__file__', 'module')}")
    if failed:
        sys.exit(1)
