"""End-to-end flows against the fakes (no network, no models): channel / playlist / video modes,
dedup, the virtual "unsorted" playlist, skipped private/live videos, fingerprint-based rebuilds and
manual additions surviving a full channel sync.

    python tests/smoke_test.py      # or: pytest tests/
"""

from __future__ import annotations

from pathlib import Path

from fakes import Harness, expect_raises, rebuilt, run_tests, vid
from ytt.youtube import Kind, detect_target


def test_detect_target() -> None:
    cases = {
        "https://www.youtube.com/@doctest/videos": (Kind.CHANNEL, None, None),
        "@doctor.ivanov": (Kind.CHANNEL, None, None),
        "UCxxxxxxxxxxxxxxxxxxxxxx": (Kind.CHANNEL, None, None),
        "https://www.youtube.com/playlist?list=PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": (
            Kind.PLAYLIST,
            "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            None,
        ),
        "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": (Kind.PLAYLIST, "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", None),
        "https://youtu.be/dQw4w9WgXcQ?si=abc": (Kind.VIDEO, None, "dQw4w9WgXcQ"),
        "https://www.youtube.com/shorts/dQw4w9WgXcQ": (Kind.VIDEO, None, "dQw4w9WgXcQ"),
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": (
            Kind.VIDEO,
            "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "dQw4w9WgXcQ",
        ),
        "dQw4w9WgXcQ": (Kind.CHANNEL, None, None),  # a bare 11-char string is a handle unless --video
    }
    for raw, (kind, playlist_id, video_id) in cases.items():
        target = detect_target(raw)
        assert (target.kind, target.playlist_id, target.video_id) == (kind, playlist_id, video_id), raw

    assert detect_target("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").note
    assert (
        detect_target(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", Kind.PLAYLIST
        ).kind
        == Kind.PLAYLIST
    )
    assert detect_target("https://youtu.be/dQw4w9WgXcQ", Kind.CHANNEL).kind == Kind.CHANNEL
    assert detect_target("dQw4w9WgXcQ", Kind.VIDEO).video_id == "dQw4w9WgXcQ"
    assert detect_target("https://www.youtube.com/watch?v=dQw4w9WgXcQ").channel is None, (
        "a video URL is not also a channel"
    )
    assert detect_target("https://youtu.be/dQw4w9WgXcQ/?t=5").video_id == "dQw4w9WgXcQ"
    assert detect_target("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ").kind == Kind.VIDEO
    assert detect_target("HTTPS://WWW.YOUTUBE.COM/@doc").kind == Kind.CHANNEL
    expect_raises(ValueError, detect_target, "https://youtu.be/not-an-id")
    uploads = detect_target("https://www.youtube.com/playlist?list=UUxxxxxxxxxxxxxxxxxxxxxx")
    assert (uploads.kind, uploads.channel) == (Kind.CHANNEL, "UCxxxxxxxxxxxxxxxxxxxxxx")
    for bad, kind in (
        ("https://google.com/watch?v=dQw4w9WgXcQ", None),
        ("@doctest", Kind.VIDEO),
        ("@doctest", Kind.PLAYLIST),
        ("", None),
    ):
        expect_raises(ValueError, detect_target, bad, kind)


def test_channel_flow() -> None:
    with Harness() as h:
        # Run 1: everything is new.
        channel, _ = h.run("@doctest")
        st = h.statuses()
        assert {k for k, v in st.items() if v == "done"} == {vid(1), vid(2), vid(3), vid(4)}
        assert st[vid(9)] == "skipped" and st[vid(5)] == "skipped"  # private, live
        assert h.downloader.downloads == 4, "a video shared by two playlists is downloaded once"
        assert h.members(h.unsorted) == [vid(4), vid(5)]
        assert set(h.digests()) == {"Сердце и сосуды.md", "Питание.md", "Без плейлиста.md", "_ALL.md"}
        everything = h.read("_ALL.md")
        assert everything.count(f"Это транскрипт файла {vid(2)}") == 1
        assert "текст уже приведён выше, в плейлисте «Сердце и сосуды»" in everything
        assert f"Это транскрипт файла {vid(2)}" in h.read("Питание.md"), (
            "per-playlist files carry the full text by default"
        )
        assert f"[00:00:00](https://youtu.be/{vid(2)}?t=0) Это транскрипт файла {vid(2)}" in everything, (
            "time codes are YouTube links"
        )
        txt = Path(h.db.get_video(vid(2))["transcript_path"]).read_text(encoding="utf-8")
        assert txt.startswith("[00:00:00] Это транскрипт") and "youtu.be" not in txt, "the .txt keeps bare [HH:MM:SS]"
        assert "видео недоступно: unavailable" in h.read("Сердце и сосуды.md")
        assert Path(h.db.get_video(vid(2))["transcript_path"]).parent.name == "Сердце и сосуды"
        assert channel["last_synced"]

        # Run 2: nothing changed on YouTube -> nothing downloaded, nothing rewritten.
        before = h.digests()
        h.run("@doctest")
        assert h.downloader.downloads == 4 and h.digests() == before

        # Run 3: a new video in an old playlist and a brand-new playlist -> only affected digests rebuilt.
        h.yt.playlists["PLaaa"][1].append(7)
        h.yt.hidden_playlists.pop("PLccc")
        h.yt.playlists["PLccc"] = ("Новый плейлист", [8])
        h.yt.tab_videos += [7, 8]
        before = h.digests()
        h.run("@doctest")
        assert h.statuses()[vid(7)] == "done" and h.statuses()[vid(8)] == "done"
        assert rebuilt(before, h.digests()) == {"Сердце и сосуды.md", "Новый плейлист.md", "_ALL.md"}

        # A playlist removed from the channel is deactivated and leaves the digests; its rows stay.
        h.yt.playlists.pop("PLccc")
        before = h.digests()
        h.run("@doctest")
        assert h.db.get_playlist("PLccc")["active"] == 0 and h.statuses()[vid(8)] == "done"
        assert "Новый плейлист" not in h.read("_ALL.md") and rebuilt(before, h.digests()) == {
            "_ALL.md",
            "Без плейлиста.md",
        }

        # A deleted transcript file is noticed and the video queued again; the kept audio is reused.
        Path(h.db.get_video(vid(3))["transcript_path"]).unlink()
        calls = h.transcriber.calls
        h.run("@doctest")
        assert h.statuses()[vid(3)] == "done" and h.transcriber.calls == calls + 1 and h.downloader.downloads == 6

        # build --force rewrites every active digest; the file of the removed playlist is left as it was.
        before = h.digests()
        h.pipeline.build(channel, force=True)
        assert rebuilt(before, h.digests()) == set(before) - {"Новый плейлист.md"}
        assert "Playlists (done/total)" in h.pipeline.status(channel)


def test_playlist_and_video_modes() -> None:
    with Harness() as h:
        # A single playlist on a fresh database: its channel is registered, only its videos are processed.
        channel, _ = h.run("https://www.youtube.com/playlist?list=PLbbb")
        assert h.statuses() == {vid(3): "done", vid(2): "done"}
        assert not channel["last_synced"]
        assert "целиком ещё не синхронизировался" in h.read("_ALL.md")
        assert set(h.digests()) == {"Питание.md", "_ALL.md"}

        # A single video that is in no known playlist -> virtual playlist.
        h.run("https://youtu.be/vid00000004?si=zzz")
        assert h.members(h.unsorted) == [vid(4)] and h.statuses()[vid(4)] == "done"

        # A video that is already done: nothing is downloaded or rebuilt, and it is not re-filed.
        before, downloads = h.digests(), h.downloader.downloads
        _, target = h.run("https://www.youtube.com/watch?v=vid00000002&list=PLbbb&index=2")
        assert target.kind == Kind.VIDEO and target.note
        assert h.members(h.unsorted) == [vid(4)] and h.digests() == before and h.downloader.downloads == downloads

        # An unlisted video (never on the Videos tab) and a live stream.
        h.run("https://www.youtube.com/watch?v=vid00000006")
        h.run("https://www.youtube.com/live/vid00000005")
        assert h.members(h.unsorted) == [vid(4), vid(6), vid(5)]
        assert h.statuses()[vid(5)] == "skipped"
        assert "Status  : skipped" in h.pipeline.status(h.channel(), video_id=vid(5))

        # Full channel sync afterwards: real playlists appear, the unlisted manual video is kept,
        # the transcript written under "Без плейлиста" is reused wherever the video shows up.
        channel, _ = h.run("@doctest")
        assert h.statuses()[vid(1)] == "done" and h.statuses()[vid(9)] == "skipped"
        assert h.members(h.unsorted) == [vid(4), vid(5), vid(6)]
        assert channel["last_synced"] and "целиком ещё не синхронизировался" not in h.read("_ALL.md")
        assert f"Это транскрипт файла {vid(2)}" in h.read("Сердце и сосуды.md")
        assert Path(h.db.get_video(vid(2))["transcript_path"]).parent.name == "Питание"

        # A playlist that is not on the channel's Playlists tab (unlisted) and contains an "unsorted" video.
        h.run("https://www.youtube.com/playlist?list=PLccc")
        assert h.members("PLccc") == [vid(8), vid(4)] and h.db.get_playlist("PLccc")["position"] == 2
        assert h.members(h.unsorted) == [vid(5), vid(6)], "video moved out of the virtual playlist"
        assert h.statuses()[vid(8)] == "done"

        # Full sync again: the manually added playlist survives, nothing else changes.
        h.run("https://www.youtube.com/@doctest/videos")
        assert h.db.get_playlist("PLccc")["active"] == 1 and h.members(h.unsorted) == [vid(5), vid(6)]
        before = h.digests()
        h.run("@doctest")
        assert h.digests() == before

        # --channel on a video URL resolves the channel through the video.
        h.yt.calls.clear()
        _, target = h.run("https://youtu.be/vid00000001", kind=Kind.CHANNEL)
        assert target.kind == Kind.CHANNEL and ("resolve_video", vid(1)) in h.yt.calls

        # build on a playlist target touches only that playlist (+ the channel digest).
        before = h.digests()
        h.pipeline.build(channel, force=True, only_playlist="PLbbb")
        assert rebuilt(before, h.digests()) == {"Питание.md", "_ALL.md"}

        # Offline lookups and error paths.
        for raw in ("https://youtu.be/vid00000004", "youtube.com/playlist?list=PLccc", "@doctest", "UC" + "x" * 22):
            assert h.pipeline.resolve_channel(detect_target(raw), online=False)["id"] == channel["id"]
        assert (
            h.pipeline.resolve_channel(detect_target("https://youtu.be/vid00000004", Kind.CHANNEL), online=False)["id"]
            == channel["id"]
        )
        expect_raises(
            LookupError,
            h.pipeline.resolve_channel,
            detect_target("PLzzzzzzzzzzzzzzzzzzzzzzzz"),
            online=False,
            contains="not in the database yet",
        )
        expect_raises(
            LookupError,
            h.pipeline.sync,
            detect_target("https://www.youtube.com/playlist?list=RDmix"),
            contains="no owner channel",
        )


if __name__ == "__main__":
    run_tests(globals())
