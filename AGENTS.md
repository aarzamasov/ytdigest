# AGENTS.md — context for an AI assistant working on yt2text

You are helping maintain **yt2text**, a small Python tool that turns a YouTube channel (or one playlist, or
one video) into text: yt-dlp for discovery, a configurable shell command for audio, local Whisper for
speech-to-text, SQLite for state, Markdown digests per playlist and per channel, incremental re-runs.
The output is uploaded by hand into an LLM workspace (Gemini) to study medical channels.

This is the cross-tool instruction file (Codex, Cursor, Copilot, Gemini CLI and others read `AGENTS.md`
on their own). Claude Code reads it too: directly when there is no `CLAUDE.md`, and here through the
one-line `CLAUDE.md` that imports it. For a chat assistant without file access, paste it as the first message.

## Load context in this order

1. `README.md` — the problem statement (requirements R1–R12), the requirement → implementation map, usage.
2. `FILES.md` — every file, its responsibility, key symbols, the DB schema, and "where to make common changes".
3. `config.toml` — the whole settings surface, with comments.
4. `tests/smoke_test.py` — the executable specification; read the scenarios to see expected behaviour.
5. Only then open the module you need under `ytt/`. Do not skim the whole package first; `FILES.md` tells you where to go.

## Owner and language

- The owner is a senior DevOps/cloud engineer. He discusses the project in **Russian** and reads code in **English**.
- Reply in Russian, concise and structured. Write **all** code, comments, docstrings, log messages, CLI help
  and commit messages in English.
- Generated Markdown is for Russian readers: the strings in `ytt/builder.py::LABELS` stay Russian.
  `README.ru.md` is the Russian user guide; keep it in sync with `README.md` when behaviour changes.

## Stack and constraints

- Python 3.11+ (`tomllib`), standard library plus `yt-dlp` and `faster-whisper`; `mlx-whisper` is optional
  (Apple Silicon). Do not add dependencies without asking.
- Must run on macOS (Apple Silicon) and Windows 11: `pathlib` everywhere, file names through
  `storage.safe_name`, UTF-8 console setup in `util.setup_logging`, forward slashes in shell templates.
- All SQL lives in `ytt/db.py`; other modules call `Database` methods.
- Settings: defaults are the dataclasses in `ytt/config.py`; `config.toml` mirrors them with comments.
- No network and no models in tests: everything is exercised through the fakes in `tests/smoke_test.py`.

## Invariants — do not break these

1. **Sync never changes processing state** except `skipped ↔ pending`. `pending/downloaded/done/error`
   belong to `Pipeline.process`.
2. **One transcript per video.** It lives in the folder of the playlist it was first processed under; its
   path is `videos.transcript_path`. Digests read the file; they never copy it.
3. **Digests are a pure function of the DB + transcript files + output options**, rewritten only when the
   fingerprint changes. If the rendered output starts depending on new data, add it to
   `playlist_fingerprint` / `channel_fingerprint`; a new output option goes into `Pipeline._render_key()`.
   Otherwise stale files will be kept.
4. **Transcripts keep bare `[HH:MM:SS]` block prefixes.** YouTube links (`youtu.be/<id>?t=<sec>`) are added only
   while rendering Markdown (`builder.linkify_timestamps`); never write links or Markdown into the `.txt`.
5. **The virtual playlist** is `__unsorted__<channel_id>`, `is_virtual = 1`, position 1 000 000 (sorts last).
   A full channel sync recomputes it as: channel-tab videos not in any active real playlist, plus `manual`
   videos not in any real playlist.
6. **Manual additions are sticky.** Playlists/videos added by their own URL carry `manual = 1`; a channel
   sync must not deactivate or drop them even when the channel tabs do not list them (unlisted content).
7. **Processing order is channel order** (`playlists.position`), virtual playlist last; videos in playlist order.
8. **Failures stay local.** A bad video → `status = error`, `attempts += 1`, retried while
   `attempts < download.max_attempts`; the run continues. Tool/model problems are detected once, before the
   first video (`Pipeline._ensure_ready`).
9. **Ctrl+C is safe.** Every step commits immediately; the next run continues from the database.
10. **Names are fixed at first sight.** `dir_name` columns are assigned once; a title change on YouTube must not
   move folders or files.
11. **Download contract.** `[download].command` is a template with `{url} {video_id} {output_dir}
    {output_template}`; success means a file `<output_dir>/<video_id>.<ext>` exists (`AUDIO_EXTS`).
12. **Removed playlists are deactivated, never deleted**; their rows and transcripts stay.

## How to verify a change

```bash
python tests/run_all.py            # every test module, no network / yt-dlp / models needed (~4 s)
python tests/run_all.py units cli  # only modules whose name contains these words
ruff check . && ruff format .      # lint and format (pip install -r requirements-dev.txt); CI runs format --check
mypy                               # static types for ytt/ and main.py — keep it at zero errors
python main.py --help              # CLI still parses
```

Occasionally, not per change: `mutmut run && mutmut results` (~10 min) to see which new code the tests do
not actually constrain; survivors that are only log or help strings can be ignored.

The suite must stay green. Behavioural changes get a scenario in the matching module (see `FILES.md`,
section `tests/`); Markdown format changes are made deliberately with `UPDATE_GOLDEN=1 python
tests/test_rendering.py` and the golden diff reviewed. CI (`.github/workflows/tests.yml`) runs the same
suite on Ubuntu, macOS and Windows.

If you cannot reach YouTube from your environment, say so explicitly: `ytt/youtube.py` is tested against
yt-dlp-shaped metadata and recorded fixtures only; the owner runs `python tests/live_check.py <channel>`
and `python tests/record_fixtures.py <channel>` himself.

## Known gaps and ideas (not commitments)

- Live YouTube discovery is unverified; yt-dlp field names may need adjusting after the first real run.
- No schema migrations: a schema change currently means deleting `data/ytt.sqlite3`. Transcript files
  survive, but nothing re-adopts them yet — every video would be transcribed again. A sensible next step is
  to re-link existing `… [video_id].txt` files during `sync`.
- Single downloads, single transcription stream; parallelism was deliberately left out for simplicity.
- Private videos need `youtube.cookies_from_browser` plus `--cookies-from-browser` in the download command.
- Possible outputs beyond Markdown (HTML/EPUB) would be new renderers in `ytt/builder.py`.

## Working agreement

- Keep changes small and focused; do not refactor neighbouring code unasked.
- When you add, remove or repurpose a file, update `FILES.md`. When behaviour changes, update the requirement
  table in `README.md`, the usage sections in both READMEs, and the tests.
- Keep this file and `CLAUDE.md` as they are: `AGENTS.md` holds the content, `CLAUDE.md` only imports it.
- Say plainly what you changed, how you tested it, and what you could not test.
- If a request is ambiguous about scope (one playlist vs. the channel, fresh DB vs. migration), ask one short
  question before building.
