"""Robustness of the pipeline when things go wrong: failing downloads and transcriptions, attempt limits
and --retry-errors, an interrupted run that is resumed, empty transcripts, --limit, keep_audio = false.
"""

from __future__ import annotations

from fakes import Harness, expect_raises, rebuilt, run_tests, vid


def test_download_failure_is_local_and_retried() -> None:
    with Harness() as h:
        h.downloader.fail_ids.add(vid(1))
        channel, _ = h.run("@doctest")
        v = h.db.get_video(vid(1))
        assert v["status"] == "error" and v["attempts"] == 1 and "simulated download failure" in v["error"]
        assert h.statuses()[vid(2)] == "done" and h.statuses()[vid(3)] == "done", "the run continued past the failure"
        assert "транскрипт пока не готов: error" in h.read("Сердце и сосуды.md")
        assert "Recent errors:" in h.pipeline.status(channel) and vid(1) in h.pipeline.status(channel)

        # Retried on the next runs until max_attempts (3), then left alone.
        h.run("@doctest")
        h.run("@doctest")
        assert h.db.get_video(vid(1))["attempts"] == 3
        downloads = h.downloader.downloads
        h.run("@doctest")
        assert h.db.get_video(vid(1))["attempts"] == 3 and h.downloader.downloads == downloads, (
            "out of attempts: not tried"
        )

        # --retry-errors resets the counter; once the cause is gone the video goes through.
        h.downloader.fail_ids.clear()
        h.run("@doctest", retry_errors=True)
        v = h.db.get_video(vid(1))
        assert v["status"] == "done" and v["attempts"] == 1 and v["error"] is None
        assert f"Это транскрипт файла {vid(1)}" in h.read("Сердце и сосуды.md")


def test_transcription_failure_and_empty_transcript() -> None:
    with Harness() as h:
        h.transcriber.fail_ids.add(vid(2))
        h.transcriber.empty_ids.add(vid(3))
        h.run("@doctest")
        assert "simulated transcription failure" in h.db.get_video(vid(2))["error"]
        assert "empty transcript" in h.db.get_video(vid(3))["error"]
        assert h.statuses()[vid(1)] == "done" and h.statuses()[vid(4)] == "done"
        # The audio of a failed transcription is kept, so the retry does not download again.
        assert (h.root / "audio" / f"{vid(2)}.mp3").exists()
        downloads = h.downloader.downloads
        h.transcriber.fail_ids.clear()
        h.transcriber.empty_ids.clear()
        h.run("@doctest")
        assert h.statuses()[vid(2)] == "done" and h.statuses()[vid(3)] == "done" and h.downloader.downloads == downloads


def test_retry_errors_scoped_to_the_target() -> None:
    with Harness() as h:
        h.downloader.fail_ids.update({vid(1), vid(3)})
        for _ in range(3):
            h.run("@doctest")
        assert h.db.get_video(vid(1))["attempts"] == 3 and h.db.get_video(vid(3))["attempts"] == 3
        h.downloader.fail_ids.clear()
        # A video target with --retry-errors resets only that video.
        h.run("https://youtu.be/vid00000003", retry_errors=True)
        assert h.statuses()[vid(3)] == "done" and h.db.get_video(vid(1))["status"] == "error"
        assert h.db.get_video(vid(1))["attempts"] == 3
        # A playlist target resets the errored videos inside it.
        h.run("https://www.youtube.com/playlist?list=PLaaa", retry_errors=True)
        assert h.statuses()[vid(1)] == "done"


def test_interrupted_run_resumes() -> None:
    with Harness() as h:
        h.downloader.interrupt_ids.add(vid(2))  # Ctrl+C while the second video downloads
        expect_raises(KeyboardInterrupt, h.run, "@doctest")
        assert h.statuses()[vid(1)] == "done"
        v2 = h.db.get_video(vid(2))
        assert v2["status"] == "pending" and v2["attempts"] == 1, "the interrupted video is not marked as an error"
        assert not h.out.exists() or "Сердце и сосуды.md" not in h.digests(), "digest not written mid-playlist"

        h.downloader.interrupt_ids.clear()
        h.run("@doctest")
        assert sum(s == "done" for s in h.statuses().values()) == 4
        assert set(h.digests()) == {"Сердце и сосуды.md", "Питание.md", "Без плейлиста.md", "_ALL.md"}


def test_limit_and_keep_audio() -> None:
    with Harness(download__keep_audio=False) as h:
        h.run("@doctest", limit=1)
        assert sum(s == "done" for s in h.statuses().values()) == 1
        assert set(h.digests()) == {"Сердце и сосуды.md", "_ALL.md"}, (
            "only the playlist worked on (and the channel) are written"
        )
        assert not list((h.root / "audio").glob("*.mp3")), "keep_audio = false removes the mp3"
        assert h.db.get_video(vid(1))["audio_path"] is None

        h.run("@doctest", limit=10)
        assert sum(s == "done" for s in h.statuses().values()) == 4
        assert set(h.digests()) == {"Сердце и сосуды.md", "Питание.md", "Без плейлиста.md", "_ALL.md"}


def test_output_option_change_triggers_rebuild() -> None:
    with Harness() as h:
        channel, _ = h.run("@doctest")
        assert "youtu.be" in h.read("_ALL.md")
        before = h.digests()
        h.run("@doctest")
        assert h.digests() == before, "same options: nothing rebuilt"
        h.cfg.output.timestamp_links = False
        h.pipeline.build(channel)
        assert set(rebuilt(before, h.digests())) == set(before), "a changed rendering option rebuilds every digest"
        assert "youtu.be" not in h.read("_ALL.md") and "[00:00:00] Это" in h.read("_ALL.md")


def test_fail_fast_before_first_video() -> None:
    with Harness() as h:

        def broken_check() -> None:
            raise RuntimeError("yt-dlp missing")

        h.downloader.check = broken_check
        expect_raises(RuntimeError, h.run, "@doctest", contains="yt-dlp missing")
        assert all(s in ("pending", "skipped") for s in h.statuses().values()), "no attempts were burnt"
        assert all(h.db.get_video(v)["attempts"] == 0 for v in h.statuses())


def test_transcript_moved_between_playlists_keeps_one_file() -> None:
    """A video processed under playlist A and later also listed in playlist B has one file, two mentions."""
    with Harness() as h:
        h.run("@doctest")
        h.yt.playlists["PLbbb"][1].append(1)  # video 1 (home: Сердце и сосуды) now also in Питание
        h.run("@doctest")
        files = [f for f in (h.root / "transcripts").rglob("*.txt") if f.stem.endswith(f"[{vid(1)}]")]
        assert len(files) == 1 and files[0].parent.name == "Сердце и сосуды"
        assert f"Это транскрипт файла {vid(1)}" in h.read("Питание.md")
        assert h.read("_ALL.md").count(f"Это транскрипт файла {vid(1)}") == 1


def test_redo_and_redo_audio() -> None:
    with Harness() as h:
        channel, _ = h.run("@doctest")
        assert h.statuses()[vid(1)] == "done"
        downloads_before = h.downloader.downloads
        transcriptions_before = h.transcriber.calls

        # --redo on channel/playlist scope without only_video raises ValueError
        expect_raises(ValueError, h.pipeline.process, channel, redo=True)

        # --redo reuses existing audio file and re-transcribes
        v1_url = f"https://www.youtube.com/watch?v={vid(1)}"
        h.run(v1_url, redo=True)
        assert h.downloader.downloads == downloads_before, "audio was reused, no new download"
        assert h.transcriber.calls == transcriptions_before + 1, "video was transcribed again"
        assert h.statuses()[vid(1)] == "done"

        # --redo_audio removes audio and downloads it fresh
        h.run(v1_url, redo_audio=True)
        assert h.downloader.downloads == downloads_before + 1, "audio was re-downloaded"
        assert h.transcriber.calls == transcriptions_before + 2, "video was transcribed again"
        assert h.statuses()[vid(1)] == "done"


if __name__ == "__main__":
    run_tests(globals())
