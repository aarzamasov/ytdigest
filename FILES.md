# FILES.md — what is where

Repository map for people and AI assistants. Read this before touching the code; update it whenever a
file is added, removed or changes its responsibility. (`AGENTS.md` tells assistants in which order to read things.)

```
yt2text/
├── main.py               CLI entry point (argparse): run | sync | process | build | status
├── config.toml           all settings with comments; every key optional (defaults live in ytt/config.py)
├── pyproject.toml        project metadata + ruff / mypy / pytest / mutmut configuration
├── requirements.txt      yt-dlp, faster-whisper (mlx-whisper commented out: Apple Silicon only)
├── requirements-dev.txt  ruff, mypy, pytest, hypothesis, mutmut, coverage (tooling only)
├── README.md             problem statement, requirement → implementation map, usage, testing (English)
├── README.ru.md          user guide in Russian (same content, different audience)
├── FILES.md              this file
├── AGENTS.md             context prompt / working agreement for AI assistants (cross-tool standard)
├── CLAUDE.md             one line: `@AGENTS.md` — Claude Code imports the file above
├── .gitignore            data/, .venv/, __pycache__/
├── .github/workflows/tests.yml   CI: offline suite on Ubuntu / macOS / Windows (3.11, 3.12) + quality job (ruff, mypy, hypothesis)
├── tests/                offline suite (no network, no yt-dlp, no models; ~3 s) + two opt-in live scripts
│   ├── fakes.py          FakeYouTube / FakeDownloader / FakeTranscriber, Harness, run_tests helper
│   ├── run_all.py        runs every test module in its own interpreter; pytest also works
│   ├── smoke_test.py     end-to-end flows: channel / playlist / video modes
│   ├── test_failures.py  failure injection, attempt limits, interrupted run, --limit, keep_audio
│   ├── test_cli.py       main.py end to end with the fakes patched in
│   ├── test_youtube_parsing.py   yt-dlp-shaped metadata, stubbed resolvers, recorded fixtures
│   ├── test_rendering.py Markdown golden files + fingerprint sensitivity
│   ├── test_units.py     config, util, DB transitions, Downloader subprocesses, naming, paragraphs, Whisper adapters
│   ├── test_properties.py   Hypothesis property tests (self-skipping when hypothesis is not installed)
│   ├── golden/           expected Markdown (regenerate with UPDATE_GOLDEN=1 python tests/test_rendering.py)
│   ├── fixtures/         recorded yt-dlp responses (created by record_fixtures.py; may be absent)
│   ├── record_fixtures.py   opt-in, network: record slimmed yt-dlp responses into fixtures/
│   └── live_check.py     opt-in, network: discovery sanity checks on a real channel, optional tiny-model transcription
├── ytt/                  the package
│   ├── __init__.py       version string only
│   ├── config.py         dataclasses + TOML loading
│   ├── youtube.py        target detection and yt-dlp metadata discovery
│   ├── db.py             SQLite schema and every query
│   ├── downloader.py     runs the configurable audio download command
│   ├── transcriber.py    Whisper backends + paragraph assembly
│   ├── storage.py        on-disk layout and safe file names
│   ├── builder.py        Markdown rendering + change fingerprints
│   ├── pipeline.py       orchestration: sync → process → build, status
│   └── util.py           time/hash/logging helpers
└── data/                 created at runtime (gitignored): SQLite DB, log, one folder per channel
```

Dependency direction (no cycles): `main → pipeline → {youtube, db, downloader, transcriber, storage, builder} → {config, util}`.
`db.py` imports `VideoInfo` from `youtube.py` for typing only.

## Files in detail

### `main.py`
- Builds the argument parser: subcommands `run`, `sync`, `process`, `build`, `status`; a positional `target`
  with a mutually exclusive `--channel / --playlist / --video` group (`dest="kind"`); `--limit`,
  `--retry-errors`, `--redo`, `--redo-audio` on `run`/`process`; `--force` on `build`; global `-c/--config`, `-v/--verbose`.
- `scope(target)` turns a `Target` into `only_playlist` / `only_video` for `Pipeline.process` / `build`.
- Loads `Config`, sets up logging (console + `data/ytt.log`), opens the `Database`, dispatches to `Pipeline`.
  Exit codes: 0 ok, 1 runtime error (clean one-line message, traceback at DEBUG), 2 bad target, 130 Ctrl+C.
- Change here when: adding a CLI option or subcommand. Business logic does not belong here.

### `ytt/config.py`
- Dataclasses `PathsConfig`, `YouTubeConfig`, `DownloadConfig`, `TranscribeConfig`, `OutputConfig`, bundled in
  `Config`. The dataclass defaults are the single source of truth; `config.toml` mirrors them with comments.
- `Config.load(path)`: explicit path, else `<repo>/config.toml` if present, else defaults. Unknown keys are
  logged and ignored. Relative `paths.data_dir` is resolved against the config file's folder.
  `transcribe.language` `""`/`"auto"` → `None` (auto-detect).
- Change here when: adding a setting. Add the field with a default, document it in `config.toml`, and mention it
  in both READMEs if it is user-facing.

### `ytt/youtube.py`
- **Target detection (pure, offline):** `Kind` (`channel` | `playlist` | `video`), `Target` dataclass
  (`kind`, `channel`, `playlist_id`, `video_id`, `note`, `label`), `detect_target(raw, kind=None)`.
  Rules: video id in the URL wins (`watch?v=`, `youtu.be/`, `/shorts/`, `/live/`, `/embed/`, `/v/`), then
  `list=` → playlist (uploads playlists `UU…` become the channel `UC…`), else channel. Bare `PL…`/`OL…` ids
  (≥ 15 chars) are playlists; a bare 11-char string is a channel handle unless `kind == VIDEO`.
  Forced kinds are validated and raise `ValueError` with a hint. Non-YouTube URLs raise `ValueError`.
- **Metadata (network, via yt-dlp Python API):** class `YouTube` with `resolve_channel(url_or_id)`,
  `list_playlists(channel_url)`, `list_playlist_videos(playlist_url)`, `list_channel_videos(channel_url)`
  (tabs from `youtube.channel_tabs`), `resolve_playlist(playlist_id) → PlaylistDetails(playlist, channel|None,
  videos)`, `resolve_video(video_id) → VideoDetails(video, channel)`.
  `_extract(url, flat=True)`: flat extraction for tabs/playlists; `flat=False` + `process=False` for one video
  (metadata only, no format selection). yt-dlp output is routed to our logger at DEBUG.
- Data classes: `ChannelInfo(id, name, url)`, `PlaylistInfo(id, title, url)`,
  `VideoInfo(id, title, url, duration, upload_date, available, live_status)` with `is_live`.
  Private/deleted entries are detected by title (`[Private video]` …) or `availability == "private"`.
- `normalize_channel_url` → canonical `https://www.youtube.com/@handle` (tabs, query, trailing slash removed).
- Change here when: YouTube/yt-dlp changes field names, a new URL shape must be recognised, or a new target
  kind is needed (then also `Pipeline.sync` and `main.scope`).

### `ytt/db.py`
- `Status` constants: `pending → downloaded → done`, `error`, `skipped`.
- `SCHEMA` (created with `IF NOT EXISTS`, no migrations yet):
  - `channels(id, url, name, dir_name, fingerprint, md_path, built_at, first_seen, last_synced)` —
    `last_synced` is NULL until the first *full* channel sync.
  - `playlists(id, channel_id, title, url, position, dir_name, is_virtual, manual, active, fingerprint,
    md_path, built_at, first_seen, last_seen)` — `is_virtual` marks the «Без плейлиста» playlist
    (`position` 1 000 000 so it sorts last), `manual` marks playlists added by their own URL (never
    deactivated by a channel sync), `active = 0` for playlists that vanished from the channel.
  - `videos(id, channel_id, title, url, duration, upload_date, status, error, attempts, audio_path,
    transcript_path, home_playlist_id, transcribed_at, manual, first_seen, last_seen)` — one row per
    video regardless of how many playlists contain it; `home_playlist_id` is the playlist whose folder
    holds the transcript.
  - `playlist_videos(playlist_id, video_id, position)` — membership and order.
- `Database` wraps every query; nothing else in the code base runs SQL. Notable methods:
  `upsert_channel`, `touch_channel_synced`, `upsert_playlist` (keeps `dir_name`, build state, `manual`),
  `deactivate_missing_playlists` (skips virtual + manual), `next_playlist_position`, `upsert_video`
  (refreshes metadata; only flips `skipped ↔ pending`), `replace_membership`, `add_membership`,
  `remove_membership`, `video_playlist_ids`, `membership_pairs`, `video_ids_in_real_playlists`,
  `manual_video_ids`, `bump_attempts`, `set_audio`, `mark_done`, `mark_error`, `reset_video`,
  `reset_errors(channel_id, video_ids=None)`, `done_videos`, `entity_counts`, `status_counts`,
  `playlist_stats`, `errored_videos`, `get_*` / `find_channel`.
- Change here when: the schema changes (then think about existing databases — today the expectation is a fresh
  `data/ytt.sqlite3`), or a new query is needed.

### `ytt/downloader.py`
- `Downloader(cfg)`: `check()` verifies the command's executable is on PATH (fail fast); `download(url, video_id,
  audio_dir)` reuses an existing `<video_id>.<ext>` (any of `AUDIO_EXTS`), otherwise formats
  `download.command` with `{url} {video_id} {output_dir} {output_template}` (unknown placeholders are left
  intact), runs it via `subprocess.run` with `timeout_sec`, and raises `DownloadError` with the last lines of
  output on failure or when no file appeared.
- Change here when: the download contract changes (new placeholder, different result detection).

### `ytt/transcriber.py`
- `Segment(start, end, text)`; `Transcriber` protocol with `transcribe(path) → list[Segment]`.
- `FasterWhisperTranscriber` (CTranslate2; `device`, `compute_type`, `beam_size`, `vad_filter`,
  `condition_on_previous_text`; progress log every 10 min of audio) and `MlxWhisperTranscriber` (Apple
  Silicon; model names mapped to `mlx-community/*` repos via `_MLX_REPOS`). `get_transcriber(cfg)` picks by
  `transcribe.backend`. Libraries are imported lazily inside the constructors.
- `segments_to_text(segments, gap_sec, max_chars, timestamps, chunk_sec)`: blocks close at the first segment
  boundary after `chunk_sec` seconds (0 = off), after a pause ≥ `gap_sec`, or when a block is over
  `max_chars` and a sentence just ended (hard cut at 2×); with `timestamps` every block starts with
  `[HH:MM:SS]` of its first segment.
- Change here when: adding a backend (implement the protocol, register it in `get_transcriber`) or changing
  paragraph rules.

### `ytt/storage.py`
- `safe_name(name, max_len)`: cross-platform file/folder names from titles — forbidden characters removed,
  whitespace collapsed, cut at a word boundary, Windows reserved names suffixed, never empty.
- `Layout(cfg, channel_dir_name)`: `root`, `audio_dir`, `transcripts_dir`, `output_dir`,
  `transcript_path(playlist_dir, position, title, video_id)` → `NNN - Title [video_id].txt`,
  `playlist_md_path(playlist_dir)`, `channel_md_path()` (`_ALL.md`).
- Change here when: the on-disk layout or naming changes.

### `ytt/builder.py`
- `LABELS`: Russian strings used inside generated Markdown (the only intentionally non-English text in the code).
- `playlist_fingerprint(playlist, videos, render_key)` and `channel_fingerprint(channel, bundles, render_key)`:
  SHA-256 over what the rendered output depends on (ids, order, titles, statuses, `transcribed_at`, the
  rendering options passed as `render_key`, and for the channel the full/partial coverage flag from
  `is_fully_synced`).
- `linkify_timestamps(text, video_id)`: `[HH:MM:SS] ` at the start of a block → `[HH:MM:SS](https://youtu.be/<id>?t=<sec>) `;
  applied by `_body` when `timestamp_links` is on. The `.txt` files are never modified.
- `render_playlist_md(channel, playlist, videos, read_text, built_at, playlist_titles, dedupe_foreign,
  membership, timestamp_links)`: `#` playlist → `##` numbered videos with URL · duration · date → text. With `dedupe_foreign`
  a video whose transcript lives under another (still listing) playlist gets a one-line reference.
- `render_channel_md(channel, bundles, read_text, built_at, timestamp_links)`: `#` channel, header, optional partial-coverage
  note, «Содержание», then `##` playlist / `###` video sections; a video seen before gets
  «текст уже приведён выше» instead of its text.
- Change here when: the Markdown format changes or a new output format is added (write a sibling renderer,
  call it from `Pipeline._build_*`).

### `ytt/pipeline.py`
- `unsorted_playlist_id(channel_id)` = `__unsorted__<channel_id>`.
- `Pipeline(cfg, db)` owns a `YouTube`, a `Downloader` and a lazily loaded `Transcriber`.
  - `resolve_channel(target, online)`: channel row for any target kind; offline first (`_find_channel_offline`),
    then YouTube (`_lookup_channel` resolves playlist/video owners), registering the channel (`_register_channel`).
  - `sync(target)` → `_sync_channel` (playlists tab → each playlist's videos → deactivate vanished playlists →
    channel tabs → recompute the virtual playlist, keeping manual videos → `touch_channel_synced` →
    `_verify_transcripts`), `_sync_playlist` (register owner if new, upsert playlist with `manual=True`, replace
    membership, remove its videos from the virtual playlist) or `_sync_video` (upsert with `manual=True`; if in
    no active playlist, append to the virtual one).
  - `process(ch, limit, retry_errors, only_playlist, only_video)`: playlists in order (narrowed by scope), for
    each video needing work: `_ensure_ready` (fail fast) → `_process_video` (download → transcribe →
    `segments_to_text` → write `.txt` → `mark_done`, errors → `mark_error`) → `_build_playlist`; then
    `_build_channel`. `_needs_work`: status not `done`/`skipped` and `attempts < max_attempts`.
  - `build(ch, force, only_playlist)`: `_build_playlist` for the scoped playlists + `_build_channel`; both
    skip writing when the fingerprint matches and the file exists (unless `force`). `_render_key()` folds the
    output options (`timestamp_links`, `dedupe_in_playlist_files`) into the fingerprints.
  - `status(ch, video_id=None)`: text report; with a video id adds that video's status and transcript path.
- Change here when: the order of operations changes or a new kind of run is needed.

### `ytt/util.py`
- `utcnow_iso`, `fmt_duration` (`1:02:03`), `fmt_ts` (`HH:MM:SS`), `sha256_text`, `setup_logging(log_file,
  verbose)` (UTF-8 console, INFO/DEBUG console + DEBUG file, noisy libraries silenced).

### `tests/` — how the suite is organised
- `fakes.py`: `FakeYouTube` (an in-memory channel the tests mutate: playlists on the tab, hidden/unlisted
  playlists, tab videos; video 9 private, 5 live, 6 unlisted), `FakeDownloader` (`fail_ids`,
  `interrupt_ids`; reuses existing audio like the real one), `FakeTranscriber` (`fail_ids`, `empty_ids`),
  `Harness` (pipeline + temp data dir; `run(raw, kind)` mirrors `main.py run`; `statuses()`, `members()`,
  `digests()` mtime snapshots, `read()`), `rebuilt()`, `expect_raises()`, `run_tests(globals())`.
  Every test module ends with `if __name__ == "__main__": run_tests(globals())`, so it runs without pytest.
- `smoke_test.py`: `test_detect_target`; `test_channel_flow` (fresh run, unchanged run, grown channel,
  removed playlist, deleted transcript, `build --force`); `test_playlist_and_video_modes` (playlist-first,
  single videos incl. unlisted and live, full sync afterwards, manual playlist survival, `--channel` on a
  video URL, scoped build, offline lookups, mix rejection).
- `test_failures.py`: download/transcription failures stay local and are retried up to `max_attempts`;
  `--retry-errors` whole channel vs. scoped to a video/playlist; `KeyboardInterrupt` mid-run leaves a
  resumable state; `--limit`; `keep_audio = false`; fail-fast before the first video; one file for a video
  that later joins a second playlist.
- `test_cli.py`: `main.main(argv)` with `ytt.pipeline.YouTube / Downloader / get_transcriber` patched to the
  fakes; a temp `config.toml` with a relative `data_dir`; exit codes 0 / 1 / 2 and argparse errors.
- `test_youtube_parsing.py`: `_video_from_entry`, `_playlist_from_entry`, `_iter_entries`, `_videos_from_info`,
  `_channel_from_info`, `normalize_channel_url` on dicts shaped like yt-dlp output; `StubYouTube` (real class,
  `_extract` replaced by a URL → dict table) for `resolve_channel` tab fall-through, `list_*`,
  `resolve_playlist`, `resolve_video`; `test_recorded_fixtures` parses whatever `fixtures/*.json` exist.
- `test_rendering.py`: `render_playlist_md` / `render_channel_md` on dict rows compared byte-for-byte with
  `golden/*.md` (full, deduped, linked, partial-coverage variants); `linkify_timestamps`; fingerprint
  determinism and sensitivity, including the render key.
- `test_units.py`: `Config.load` (relative/absolute `data_dir`, unknown keys, `language` normalisation);
  `util`; DB status-transition table, playlist/membership queries, `manual`/virtual protection;
  `Downloader` with real subprocesses (success, reuse, non-zero exit, no file, timeout, missing binary);
  `safe_name` + `Layout` with a seeded random property check; `segments_to_text` rules + property check;
  `FasterWhisperTranscriber` / `MlxWhisperTranscriber` against stand-in modules injected into `sys.modules`,
  import-error messages, unknown backend.
- `test_properties.py`: Hypothesis properties — `safe_name` invariants (length, charset, idempotence, device names),
  `segments_to_text` (all words kept in order, every block time-coded, hard cut honoured), `linkify_timestamps`
  round trip, URL/id detection for generated ids, `normalize_channel_url` idempotence, time formatting round
  trips, fingerprint sensitivity to order/membership. Hypothesis found two real `safe_name` edge cases.
- Quality tooling: `ruff check .` / `ruff format --check .` (the code is ruff-formatted), `mypy` (`ytt/` + `main.py`,
  `check_untyped_defs`), `mutmut run` (sources `ytt/` + `main.py`, tests under `tests/`; results in `mutants/`,
  gitignored). The last full mutation run: 3344 mutants, 2251 killed, 1036 survived (mostly log/help strings).
- Opt-in scripts (need network): `record_fixtures.py <channel>` (Videos tab, Playlists tab, first playlist,
  first video page → `fixtures/<channel>-<kind>.json`, bulky keys dropped, entries truncated);
  `live_check.py <channel> [--transcribe] [--cookies-from-browser X] [--model tiny]` (sanity checks on real
  metadata; with `--transcribe` the shortest video goes through download + Whisper in a temp folder).

## Runtime data (`data/`, gitignored)

```
data/ytt.sqlite3                                   the database (WAL mode)
data/ytt.log                                       DEBUG log
data/<Channel>/audio/<video_id>.mp3                downloaded audio (deleted when keep_audio = false)
data/<Channel>/transcripts/<Playlist>/NNN - Title [video_id].txt
data/<Channel>/output/<Playlist>.md                per-playlist digest
data/<Channel>/output/_ALL.md                      whole-channel digest
```

Folder names come from `safe_name(title, output.max_name_len)`; collisions get a ` [last 8 chars of id]`
suffix. Names are fixed at first sight (`dir_name` columns) so renames on YouTube do not move files.

## Where to make common changes

| Change | Touch |
|---|---|
| New CLI option | `main.py` (parser + dispatch), then `Pipeline` signature |
| New setting | `config.py` dataclass default → `config.toml` comment → READMEs |
| New transcription engine | `transcriber.py`: class implementing `Transcriber`, register in `get_transcriber` |
| Different audio tool/format | `config.toml [download].command` only; `AUDIO_EXTS` in `downloader.py` if a new extension |
| Markdown format | `builder.py` renderers (+ `LABELS`); fingerprints only if the output depends on new data |
| New URL shape / target kind | `youtube.py detect_target` (+ `Pipeline.sync`, `main.scope` for a new kind) |
| Schema change | `db.py SCHEMA` + queries; decide on migration vs. fresh DB and say so in the READMEs |
| Anything behavioural | add or adjust a scenario: flows → `smoke_test.py`, error paths → `test_failures.py`, CLI → `test_cli.py`, metadata shapes → `test_youtube_parsing.py`, output format → `test_rendering.py` (+ `UPDATE_GOLDEN=1`), helpers → `test_units.py` |
| Markdown format | also regenerate `tests/golden/` with `UPDATE_GOLDEN=1 python tests/test_rendering.py` and review the diff |
