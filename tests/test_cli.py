"""main.py end to end: argument parsing, exit codes, config file handling and the printed status —
with the network, downloader and model replaced by the fakes (patched where Pipeline looks them up).
"""

from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # `main` lives in the repo root

import main as cli
from fakes import CHANNEL_DIR, FakeDownloader, FakeTranscriber, FakeYouTube, expect_raises, run_tests, vid
from ytt.db import Database


class Cli:
    """Runs `main.main(argv)` with a temp config/data dir and the fakes patched in."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ytt-cli-"))
        self.config = self.tmp / "config.toml"
        self.config.write_text('[paths]\ndata_dir = "data"\n', encoding="utf-8")  # relative -> next to the config
        self.patches = [
            mock.patch("ytt.pipeline.YouTube", FakeYouTube),
            mock.patch("ytt.pipeline.Downloader", FakeDownloader),
            mock.patch("ytt.pipeline.get_transcriber", lambda cfg: FakeTranscriber()),
        ]

    def __enter__(self) -> Cli:
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __call__(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):  # argparse errors go to stderr
            code = cli.main(["-c", str(self.config), *argv])
        return code, out.getvalue()

    def statuses(self) -> dict[str, str]:
        db = Database(self.tmp / "data" / "ytt.sqlite3")
        try:
            return {row[0]: row[1] for row in db.conn.execute("SELECT id, status FROM videos")}
        finally:
            db.close()


def test_run_limit_then_full() -> None:
    with Cli() as cli_:
        code, out = cli_("run", "@doctest", "--limit", "2")
        assert code == 0 and "Playlists (done/total)" in out
        assert sum(s == "done" for s in cli_.statuses().values()) == 2
        assert (cli_.tmp / "data" / "ytt.sqlite3").exists(), "relative data_dir resolved against the config file"

        code, out = cli_("run", "@doctest")
        assert code == 0 and sum(s == "done" for s in cli_.statuses().values()) == 4
        assert (cli_.tmp / "data" / CHANNEL_DIR / "output" / "_ALL.md").exists()


def test_offline_commands() -> None:
    with Cli() as cli_:
        assert cli_("sync", "@doctest")[0] == 0
        assert cli_.statuses() and all(s in ("pending", "skipped") for s in cli_.statuses().values())
        assert cli_("process", "@doctest")[0] == 0 and sum(s == "done" for s in cli_.statuses().values()) == 4
        code, out = cli_("status", "https://youtu.be/vid00000004")
        assert code == 0 and "Video   : Видео без плейлиста" in out and "Status  : done" in out
        assert cli_("build", "@doctest", "--force")[0] == 0
        assert cli_("build", "https://www.youtube.com/playlist?list=PLbbb")[0] == 0
        assert cli_("run", "https://www.youtube.com/playlist?list=PLccc", "--retry-errors")[0] == 0
        assert cli_.statuses()[vid(8)] == "done"
        assert cli_("-v", "status", "@doctest")[0] == 0, "verbose flag accepted"


def test_exit_codes() -> None:
    with Cli() as cli_:
        assert cli_("run", "https://google.com/watch?v=x")[0] == 2, "not a YouTube URL"
        assert cli_("run", "@doctest", "--video")[0] == 2, "forced kind without an id"
        assert cli_("status", "https://www.youtube.com/playlist?list=PLnope0000000000000000000")[0] == 1, (
            "unknown playlist offline"
        )
        exc = expect_raises(SystemExit, cli_, "run", "x", "--video", "--playlist")
        assert exc.code == 2, "mutually exclusive kind flags are rejected by argparse"
        exc = expect_raises(SystemExit, cli_)
        assert exc.code == 2, "a subcommand is required"


if __name__ == "__main__":
    run_tests(globals())
