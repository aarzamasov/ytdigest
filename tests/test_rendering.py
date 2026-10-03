"""Markdown rendering and change fingerprints, without a database: rows are plain dicts.

Golden files in tests/golden/ pin the exact output format. When a format change is intentional,
regenerate them with `UPDATE_GOLDEN=1 python tests/test_rendering.py` and review the diff.
"""

from __future__ import annotations

import os
from pathlib import Path

from fakes import run_tests
from ytt.builder import (
    channel_fingerprint,
    is_fully_synced,
    linkify_timestamps,
    playlist_fingerprint,
    render_channel_md,
    render_playlist_md,
)

GOLDEN = Path(__file__).resolve().parent / "golden"
BUILT_AT = "2026-01-02T03:04:05+00:00"
UC = "UC" + "g" * 22


def channel_row(full: bool = True) -> dict:
    return {
        "id": UC,
        "name": "Доктор Тест",
        "url": "https://www.youtube.com/@doctest",
        "last_synced": BUILT_AT if full else None,
        "fingerprint": None,
    }


def playlist_row(pid: str, title: str) -> dict:
    return {"id": pid, "title": title, "url": f"https://www.youtube.com/playlist?list={pid}", "dir_name": title}


def video_row(n: int, position: int, status: str = "done", home: str | None = "PLheart", **extra) -> dict:
    row = {
        "id": f"vid{n:08d}",
        "title": f"Видео {n}",
        "url": f"https://www.youtube.com/watch?v=vid{n:08d}",
        "duration": 60 * n + 5,
        "upload_date": f"2025-02-0{n}",
        "status": status,
        "error": None,
        "transcript_path": f"/x/{n}.txt" if status == "done" else None,
        "home_playlist_id": home if status == "done" else None,
        "position": position,
        "transcribed_at": BUILT_AT if status == "done" else None,
    }
    row.update(extra)
    return row


TEXTS = {f"vid{n:08d}": f"Первый абзац видео {n}.\n\nВторой абзац видео {n}." for n in range(1, 6)}
TEXTS["vid00000001"] = (
    "[00:00:00] Первый абзац видео 1.\n\n[00:12:35] Второй абзац видео 1.\n\n[01:02:03] Третий абзац видео 1."
)


def read_text(row: dict) -> str | None:
    return TEXTS.get(row["id"])


def bundles():
    heart = playlist_row("PLheart", "Сердце и сосуды")
    food = playlist_row("PLfood", "Питание")
    unsorted = {**playlist_row("__unsorted__" + UC, "Без плейлиста"), "url": "https://www.youtube.com/@doctest/videos"}
    heart_videos = [
        video_row(1, 0),
        video_row(2, 1),
        video_row(
            3, 2, status="skipped", home=None, error="unavailable (private or deleted)", duration=None, upload_date=None
        ),
    ]
    food_videos = [video_row(4, 0, home="PLfood"), video_row(2, 1)]  # video 2 is shared, text lives under PLheart
    unsorted_videos = [video_row(5, 0, status="pending", home=None)]
    return [(heart, heart_videos), (food, food_videos), (unsorted, unsorted_videos)]


def check_golden(name: str, actual: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        print(f"      updated {path.name}")
        return
    assert path.exists(), f"{path} missing — run with UPDATE_GOLDEN=1 to create it"
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, f"{name} differs from the golden file (set UPDATE_GOLDEN=1 if the change is intended)"


def test_playlist_markdown_golden() -> None:
    (heart, heart_videos), (food, food_videos), _ = bundles()
    titles = {heart["id"]: heart["title"], food["id"]: food["title"]}
    full = render_playlist_md(channel_row(), food, food_videos, read_text, BUILT_AT, titles, dedupe_foreign=False)
    check_golden("playlist_full.md", full)
    linked = render_playlist_md(channel_row(), heart, heart_videos, read_text, BUILT_AT, titles, timestamp_links=True)
    check_golden("playlist_linked.md", linked)
    assert "[00:00:00](https://youtu.be/vid00000001?t=0) Первый абзац видео 1." in linked
    assert "Первый абзац видео 2." in full, "without dedupe the shared video's text is repeated"

    membership = {("PLheart", "vid00000002"), ("PLfood", "vid00000002"), ("PLfood", "vid00000004")}
    deduped = render_playlist_md(
        channel_row(), food, food_videos, read_text, BUILT_AT, titles, dedupe_foreign=True, membership=membership
    )
    check_golden("playlist_deduped.md", deduped)
    assert "текст приведён в файле плейлиста «Сердце и сосуды»" in deduped and "Первый абзац видео 2." not in deduped

    # The reference is dropped when the home playlist no longer lists the video (or is unknown).
    dangling = render_playlist_md(
        channel_row(),
        food,
        food_videos,
        read_text,
        BUILT_AT,
        titles,
        dedupe_foreign=True,
        membership={("PLfood", "vid00000002")},
    )
    assert "Первый абзац видео 2." in dangling
    unknown_home = render_playlist_md(
        channel_row(), food, food_videos, read_text, BUILT_AT, {food["id"]: "Питание"}, dedupe_foreign=True
    )
    assert "Первый абзац видео 2." in unknown_home


def test_linkify_timestamps() -> None:
    text = "[00:00:00] Начало.\n\n[00:12:35] Середина [не метка] текста.\n\n[01:02:03] Конец.\nне в начале [00:00:05] строки"
    linked = linkify_timestamps(text, "dQw4w9WgXcQ")
    assert linked == (
        "[00:00:00](https://youtu.be/dQw4w9WgXcQ?t=0) Начало.\n\n"
        "[00:12:35](https://youtu.be/dQw4w9WgXcQ?t=755) Середина [не метка] текста.\n\n"
        "[01:02:03](https://youtu.be/dQw4w9WgXcQ?t=3723) Конец.\nне в начале [00:00:05] строки"
    )
    assert linkify_timestamps("без меток", "x") == "без меток"
    assert linkify_timestamps(linked, "dQw4w9WgXcQ") == linked, "already linked text is left alone"


def test_channel_markdown_golden() -> None:
    full = render_channel_md(channel_row(), bundles(), read_text, BUILT_AT)
    check_golden("channel_full.md", full)
    assert "[00:12:35] Второй абзац видео 1." in full and "youtu.be" not in full, "plain time codes without the option"
    linked = render_channel_md(channel_row(), bundles(), read_text, BUILT_AT, timestamp_links=True)
    check_golden("channel_linked.md", linked)
    assert "[00:12:35](https://youtu.be/vid00000001?t=755) Второй абзац видео 1." in linked
    assert "[01:02:03](https://youtu.be/vid00000001?t=3723) Третий" in linked
    assert full.count("Первый абзац видео 2.") == 1
    assert "текст уже приведён выше, в плейлисте «Сердце и сосуды»" in full
    assert "_(видео недоступно: unavailable (private or deleted))_" in full
    assert "_(транскрипт пока не готов: pending)_" in full
    assert "Плейлистов: 3 · Видео: 5" in full, "shared video counted once"
    assert "целиком ещё не синхронизировался" not in full

    partial = render_channel_md(channel_row(full=False), bundles(), read_text, BUILT_AT)
    check_golden("channel_partial.md", partial)
    assert "целиком ещё не синхронизировался" in partial


def test_fingerprints() -> None:
    (heart, videos), _, _ = bundles()
    base = playlist_fingerprint(heart, videos)
    assert base == playlist_fingerprint(heart, [dict(v) for v in videos]), "deterministic"
    assert base != playlist_fingerprint({**heart, "title": "Другое название"}, videos), "title change"
    assert base != playlist_fingerprint(heart, videos[::-1]), "order change"
    assert base != playlist_fingerprint(heart, [{**videos[0], "status": "error"}, *videos[1:]]), "status change"
    assert base != playlist_fingerprint(heart, [{**videos[0], "transcribed_at": "later"}, *videos[1:]]), (
        "re-transcribed"
    )
    assert base != playlist_fingerprint(heart, videos[:-1]), "membership change"
    assert base == playlist_fingerprint(heart, [{**videos[0], "duration": 1}, *videos[1:]]), (
        "metadata not in the digest body does not count"
    )

    assert base != playlist_fingerprint(heart, videos, render_key="links=1"), "rendering options are part of the key"
    assert playlist_fingerprint(heart, videos, "links=1") == playlist_fingerprint(heart, videos, "links=1")

    channel_full = channel_fingerprint(channel_row(), bundles())
    assert channel_full != channel_fingerprint(channel_row(), bundles(), render_key="links=1")
    assert channel_full == channel_fingerprint({**channel_row(), "last_synced": "any other time"}, bundles()), (
        "the sync time itself must not force a rebuild"
    )
    assert channel_full != channel_fingerprint(channel_row(full=False), bundles()), "partial -> full coverage rebuilds"
    assert channel_full != channel_fingerprint({**channel_row(), "name": "Renamed"}, bundles())
    assert is_fully_synced(channel_row()) and not is_fully_synced(channel_row(full=False))


if __name__ == "__main__":
    run_tests(globals())
