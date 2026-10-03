# yt2text — a YouTube channel as text

Turns a YouTube channel (or a single playlist, or a single video) into plain-text transcripts and
Markdown digests: download the audio, transcribe it locally with Whisper, assemble one Markdown file
per playlist and one for the whole channel. Re-running is cheap: only new videos are fetched and only
changed digests are rewritten.

Purpose: produce a text version of (medical) YouTube channels that can be uploaded by hand into an LLM
workspace such as Gemini and queried there.

- Russian user guide: [README.ru.md](README.ru.md)
- File-by-file map of the code: [FILES.md](FILES.md)
- Context prompt for AI assistants working on the repo: [AGENTS.md](AGENTS.md) (`CLAUDE.md` just imports it)

## Problem statement

Input: a YouTube channel. The tool must:

| # | Requirement |
|---|---|
| R1 | Accept a channel as URL, `@handle` or `UC…` id; **also** accept a single playlist URL or a single video URL, detecting the kind automatically, with flags to force the interpretation. |
| R2 | Find all playlists of the channel and all video links inside each playlist; keep everything in SQLite. |
| R3 | Walk the playlists in channel order and download each video's audio as MP3 using a user-supplied, configurable shell command. |
| R4 | Feed the audio into a speech-to-text engine (engine choice left to the implementer) and get plain text. |
| R5 | Store the text as files in a `channel / playlist / video` folder structure. |
| R6 | After each playlist, write one Markdown file: playlist title → for each video its title and link → the full text. |
| R7 | After all playlists, write one Markdown file for the whole channel with the same structure. |
| R8 | Also list every video of the channel; videos that belong to no playlist are processed as a group of their own, after the real playlists. |
| R9 | A video that appears in several playlists is downloaded and transcribed once and its text is not repeated in the channel digest. |
| R10 | Re-runs are incremental: fetch only new videos and new playlists, rebuild a digest only if something in it changed. |
| R11 | Playlist / video runs work on their own without a full channel sync; what was added that way must survive later channel syncs. |
| R12 | Output is meant for a Russian-speaking reader and an LLM: Russian section labels, readable text, stable links. |
| R13 | Transcripts are written in short time-coded blocks (about 10 s each, aligned to sentence boundaries); in the Markdown digests every time code is a link that opens the video at that second, so a passage found in the text can be checked on YouTube without searching. |

## Implementation status

Everything above is implemented. Where each requirement lives and how it is verified:

| # | Where | Verified by |
|---|---|---|
| R1 | `ytt/youtube.py` — `detect_target`, `Target`, `Kind`; CLI flags `--channel/--playlist/--video` in `main.py` | `smoke_test.py::test_detect_target`, `test_cli.py` |
| R2 | `ytt/youtube.py` — `YouTube.list_playlists / list_playlist_videos / list_channel_videos` (yt-dlp flat extraction); `ytt/db.py` schema | `test_youtube_parsing.py`, `smoke_test.py::test_channel_flow`, `test_units.py::test_db_*` |
| R3 | `ytt/pipeline.py` — `Pipeline.process` iterates `db.active_playlists` in position order; `ytt/downloader.py` runs `[download].command` from `config.toml` | `test_channel_flow`, `test_units.py::test_downloader_subprocess_paths` |
| R4 | `ytt/transcriber.py` — `FasterWhisperTranscriber` (CPU / NVIDIA) and `MlxWhisperTranscriber` (Apple Silicon); default model `large-v3-turbo`, `language = "ru"` | `test_units.py::test_whisper_adapters_with_stand_in_libraries` (adapters against stand-in libraries; real models only via `tests/live_check.py --transcribe`) |
| R5 | `ytt/storage.py` — `Layout`: `data/<Channel>/transcripts/<Playlist>/NNN - Title [video_id].txt`, Windows-safe names | `test_channel_flow`, `test_units.py::test_safe_name_and_layout` |
| R6 | `ytt/builder.py` — `render_playlist_md`; written by `Pipeline._build_playlist` right after each playlist | `test_rendering.py` (golden files), `test_channel_flow` |
| R7 | `ytt/builder.py` — `render_channel_md` → `output/_ALL.md`; written by `Pipeline._build_channel` | `test_rendering.py` (golden files), `test_channel_flow` |
| R8 | `Pipeline._sync_channel`: videos from the channel tabs minus videos in real playlists → virtual playlist `__unsorted__<channel_id>` titled «Без плейлиста», sorted last | `test_channel_flow` |
| R9 | Status is per video (`videos.status`), not per membership; `render_channel_md` prints the text once and a «текст уже приведён выше» reference afterwards | `test_channel_flow` (shared video downloaded once, text once in `_ALL.md`), `test_failures.py::test_transcript_moved_between_playlists_keeps_one_file` |
| R10 | `upsert_*` keep processing state; `playlist_fingerprint` / `channel_fingerprint` (SHA-256 over ids, order, statuses, transcript times, titles) decide what to rewrite | `test_channel_flow` (second run rewrites nothing; third run rewrites only affected digests), `test_rendering.py::test_fingerprints`, `test_units.py::test_db_video_status_transitions` |
| R11 | `Pipeline._sync_playlist` / `_sync_video`; `manual` flags in `playlists` / `videos`; partial-coverage note in `_ALL.md` while `channels.last_synced` is NULL | `test_playlist_and_video_modes` |
| R12 | `builder.LABELS` (Russian), blocks from Whisper segments (`segments_to_text`: time-, pause- and length-based) | `test_units.py::test_segments_to_text_rules_and_properties`, golden files in `tests/golden/` |
| R13 | `segments_to_text(chunk_sec=10, timestamps=True)` writes `[HH:MM:SS] text` blocks into the `.txt`; `builder.linkify_timestamps` turns them into `[HH:MM:SS](https://youtu.be/<id>?t=<sec>)` when rendering digests (`output.timestamp_links`); the option is part of the fingerprints (`Pipeline._render_key`) | `test_units.py` (chunking), `test_rendering.py::test_linkify_timestamps` + `golden/*_linked.md`, `smoke_test.py`, `test_failures.py::test_output_option_change_triggers_rebuild` |

Everything in that table runs offline: `python tests/run_all.py` (about 3 seconds, no network, no yt-dlp,
no models). See [Testing](#testing) for the layers and the two opt-in live scripts.

**Not verified live.** The YouTube discovery code is tested against yt-dlp-shaped metadata and recorded
fixtures, not against the live site; the first run against a real channel is the first live test
(`python tests/live_check.py <channel>` does exactly that, in a temporary folder).
If `sync` returns no playlists or no videos: `pip install -U yt-dlp`, then set
`youtube.cookies_from_browser` in `config.toml` if YouTube asks to sign in.

## How it works

```
run <target>  =  sync  →  process  →  build

sync     discover metadata (yt-dlp, no downloads) and upsert it into SQLite
process  for each playlist in order: for each unprocessed video: download audio → transcribe → write .txt;
         rebuild the playlist digest when the playlist changed
build    rebuild the channel digest when anything changed
```

Three target kinds (auto-detected from the URL, see `detect_target`):

| Target | Scope of `run` |
|---|---|
| channel — `@handle`, `UC…`, channel URL (any tab) | every playlist, every video outside playlists, all digests |
| playlist — `…/playlist?list=PL…`, bare `PL…` | that playlist only; owner channel registered automatically |
| video — `watch?v=`, `youtu.be/`, `/shorts/`, `/live/` | that video only; filed under «Без плейлиста» until a full channel sync places it (YouTube does not expose which playlists contain a video) |

Rules of thumb baked into the design:

- **Processing state belongs to the pipeline.** `sync` only inserts new rows and refreshes metadata; the only
  status change it makes is `skipped ↔ pending` (a private video became public, a stream ended, …).
- **One video, one transcript.** The `.txt` lives in the folder of the playlist it was first processed under;
  every digest that mentions the video reads it from `videos.transcript_path`.
- **The `.txt` is the source, links are added at render time.** Transcripts hold bare `[HH:MM:SS]` block
  prefixes (compact, grep-friendly); `build` converts them into `youtu.be/<id>?t=<sec>` links in the Markdown.
  Flipping `output.timestamp_links` or `output.dedupe_in_playlist_files` changes the fingerprints, so the
  next run rewrites the digests.
- **Digests are a pure function of the database.** They are regenerated from DB rows + transcript files;
  fingerprints only decide whether writing is necessary. `build --force` rewrites everything.
- **Failures are local.** A failing video is marked `error` and retried on later runs up to
  `download.max_attempts`; `--retry-errors` resets the counters. Missing executables and model loading are
  checked once before the first video (fail fast). Ctrl+C is safe; the next run continues.
- **Manual additions are sticky.** A playlist or video added by its own URL is flagged `manual` and is not
  deactivated or dropped by a channel sync even if the channel tabs do not list it (unlisted content).
- **Removed playlists are kept but inactive** (`playlists.active = 0`): their transcripts stay on disk, they
  simply leave the digests.

## Quick start

Requires Python 3.11+ and ffmpeg (`brew install ffmpeg` / `winget install Gyan.FFmpeg`).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install mlx-whisper                                 # Apple Silicon only; then backend = "mlx_whisper"

python main.py run @channel --limit 3                   # test drive on three videos
python main.py run @channel                             # full channel
python main.py run "https://www.youtube.com/playlist?list=PL…"   # one playlist
python main.py run "https://youtu.be/…"                 # one video
python main.py run "https://youtu.be/…" --redo          # re-transcribe one video (reuses audio if present)
python main.py run "https://youtu.be/…" --redo-audio    # re-download audio and re-transcribe one video
python main.py status @channel                          # progress; also accepts a video / playlist URL
python main.py run @channel --retry-errors              # retry failed videos
python main.py build @channel --force                   # rewrite all digests
python main.py sync @channel                            # metadata only, nothing downloaded
```

Flags `--channel`, `--playlist`, `--video` override detection; `--channel <video URL>` means "the whole
channel this video belongs to". Every command accepts `-c path/to/config.toml` and `-v` (debug output).

## Configuration

All options with comments: [config.toml](config.toml). The ones that matter most:

| Key | Purpose |
|---|---|
| `download.command` | the shell command that produces the audio file; placeholders `{url} {video_id} {output_dir} {output_template}`; keep `-o "{output_template}"` so the result can be found |
| `transcribe.backend` / `model` | `faster_whisper` or `mlx_whisper`; `large-v3-turbo` is the default |
| `transcribe.language` | `"ru"`; empty string → auto-detect per video |
| `transcribe.chunk_sec` | length of a time-coded block, default 10 s; a block closes at the next Whisper segment boundary, so sentences stay whole; `0` = split only by pauses/length |
| `transcribe.timestamps` | `true` (default) → every block starts with `[HH:MM:SS]`; `false` → plain text and no links |
| `output.timestamp_links` | `true` (default) → in `.md` files the time code is a link to the video at that second |
| `youtube.channel_tabs` | `["videos"]`; add `"shorts"`, `"streams"` to pick those up too |
| `youtube.cookies_from_browser` | `"chrome"` etc. when YouTube requires a login for listing |
| `download.keep_audio` | `false` → delete the MP3 after a successful transcription |
| `output.dedupe_in_playlist_files` | `true` → per-playlist digests reference instead of repeating a video transcribed under another playlist |

## Data layout

```
data/
├── ytt.sqlite3                      channels, playlists, videos, membership, statuses, fingerprints
├── ytt.log                          DEBUG log of every run
└── <Channel>/
    ├── audio/<video_id>.mp3
    ├── transcripts/<Playlist>/NNN - Title [video_id].txt
    └── output/
        ├── <Playlist>.md
        └── _ALL.md                  the whole channel (upload this)
```

## Testing

```bash
python tests/run_all.py                      # all modules, each in its own interpreter; exit code 1 on failure
python tests/run_all.py failures rendering   # a subset, by file-name words
python tests/smoke_test.py                   # any module runs on its own
pytest tests/                                # also works if pytest is installed (not required)
```

| Layer | Where | What it protects |
|---|---|---|
| End-to-end flows with fakes | `tests/smoke_test.py` | channel / playlist / video modes, dedup, virtual playlist, incremental rebuilds, manual additions |
| Failure injection | `tests/test_failures.py` | failing downloads/transcriptions, attempt limits, `--retry-errors` scope, an interrupted run that resumes, `--limit`, `keep_audio` |
| CLI | `tests/test_cli.py` | `main.py` end to end: arguments, exit codes, config resolution, printed status |
| yt-dlp metadata parsing | `tests/test_youtube_parsing.py` | parsers and resolvers against yt-dlp-shaped dicts, plus any recorded fixtures in `tests/fixtures/` |
| Rendering | `tests/test_rendering.py` + `tests/golden/*.md` | exact Markdown format (golden files) and fingerprint sensitivity |
| Units | `tests/test_units.py` | config loading (incl. the unknown-key warning), DB status transitions and membership, the real `Downloader` subprocess paths, exact boundaries of `safe_name` / `segments_to_text` plus seeded randomized checks, Whisper adapters against stand-in libraries |
| Property-based | `tests/test_properties.py` | Hypothesis strategies over names, segment lists, time codes, URLs and fingerprints; skips itself when `hypothesis` is not installed |

Opt-in, need network: `python tests/record_fixtures.py <channel>` stores slimmed real yt-dlp responses in
`tests/fixtures/` (commit them; the parsing test picks them up — rerun after a yt-dlp upgrade);
`python tests/live_check.py <channel> [--transcribe]` checks discovery against the live site and, with the
flag, downloads and transcribes the channel's shortest video with the `tiny` model into a temp folder.
`.github/workflows/tests.yml` runs the offline suite on Ubuntu, macOS and Windows with Python 3.11 and 3.12,
plus a `quality` job on Ubuntu with the dev tooling installed.

Quality gates (config in `pyproject.toml`, tools in `requirements-dev.txt`):

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .   # lint + formatting (the code base is ruff-formatted)
mypy                                    # static types for ytt/ and main.py
coverage run tests/run_all.py && coverage report -m
mutmut run && mutmut results            # mutation testing, ~10 min; run occasionally, not in CI
```

Mutation testing is the honesty check on the suite itself: mutmut changes the code in small ways and expects
a test to fail. The last full run mutated 3344 sites; 2251 were killed, 57 had no covering test and 1036
survived — most survivors are log and help-string edits, but the run also pointed at unguarded comparison
boundaries in `segments_to_text`, `safe_name` and `detect_target`, which now have exact-boundary tests.

## Limitations

- YouTube mixes (`RD…`), «Watch later» and «Liked videos» have no owner channel and are rejected.
- A video given on its own cannot be assigned to its real playlist until the channel is synced in full.
- Playlists on a channel's Playlists tab are attributed to that channel even if they were saved from another
  channel; a playlist stays with the channel it was first filed under.
- Very large channels produce a very large `_ALL.md`; upload per-playlist files instead when it exceeds the
  model's context.
- The digest file of a playlist that disappeared from the channel stays in `output/` unchanged (its content
  is still valid); only `_ALL.md` drops it.
- No schema migrations yet: after a schema change delete `data/ytt.sqlite3`; transcripts are then processed
  again, because existing `.txt` files are not re-adopted.
