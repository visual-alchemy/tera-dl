# Changelog

All notable changes to tera-dl.

## [Unreleased]

### Added
- TUI: `?` help modal listing every keybinding, and `t` to toggle theme
- TUI: adjustable pane split with `[` / `]` (30–70%)
- TUI: transfer panel with a progress bar for uploads and downloads
- TUI: sortable file table — `s` cycles name/size/time, asc/desc, dirs first
- TUI: pane border titles with `~`-relative path, item count and marked count
- TUI: `(loading...)` and `(empty)` placeholders, marked rows highlighted,
  middle-truncated filenames that keep their extension
- TUI: status bar split into path/message and selected-file info (size, date)

### Changed
- TUI styles moved to `tera/tui.tcss`; panes use border titles instead of a
  separate header row, saving a line per pane
- TUI file list migrated to `DataTable` with Name/Size/Modified columns
  (right-aligned sizes, date column, row cursor)
- Pane listing no longer calls `query_one` from the worker thread; the worker
  returns entries and the UI updates on the main thread

## [0.3.0] — 2026-11-22

### Added
- `a` keybinding — select all files in active pane (toggle)
- Recursive folder download in TUI — F5 on a remote directory walks the tree
  and preserves the folder structure locally
- Matcher fallbacks for `(1)`, trailing digits, and Instagram post-ID suffixes
  before giving up on a folder match
- Update README: `pipx` install option, TUI keybinding reference, auth flow docs

### Changed
- `format_size` now lives only in `formatter`; `downloader` imports it
- `tera info` shows the same account panel as `tera auth status`
  (username, storage, percent) instead of one storage line
- Progress columns (speed/bytes/ETA by terminal width) are shared by
  uploads and downloads; download filenames now use the available width
  instead of a fixed 20-character cap
- TUI pane header shows the item count; secondary keybindings (`v`, `r`,
  `a`, `Insert`, `F7`) are hidden from the footer so it fits narrow screens
- Download output no longer repeats a success line per file plus a summary
- Removed the unused `chunk_size` config field (old configs still load)
- TUI uploads now use `uploader.upload_file` through an `on_progress` callback
  instead of a second copy of the upload protocol, so they get the same
  rate-limit backoff, connection retries, and errno checks as `tera ul`/`sync`

### Fixed
- `tera view` on Android never used its temp-download path — a bad `tasks=`
  kwarg raised `TypeError` that was swallowed by the broad `except`, so every
  view fell back to direct streaming
- Config load no longer resets to a default, auth-less config when
  `config.json` contains an unknown key (used to raise `TypeError`)
- Drive-path downloads strip a trailing `/`, so folders are detected and
  downloaded instead of hitting `get_download_link` and 403ing
- Stopped writing `bdstoken` and session cookies into
  `tui_upload_debug.log`
- `git safe.directory` issue in Termux environment
- Read timeouts and dropped chunked responses are retried with backoff like
  connection errors (previously one slow read aborted `tera ul`)
- Errors no longer echo `jsToken`/`bdstoken` (redacted in request errors)
- `api_errors.log` logs only errno/errmsg, not full responses, and all debug
  logs are capped instead of growing forever

---

## [0.2.0] — 2026-06-29

### Added
- **MC-style dual-pane TUI** (`tera tui`) — textual-based file manager
  - Local + remote panes side by side
  - `Tab` switch panes, `Enter`/`→` open folder, `←`/`Backspace` go up
  - `Space`/`Insert` mark files for batch operations
  - `F5` batch copy (upload/download marked or selected files)
  - `F7` mkdir, `F8` batch delete, `F2` rename
  - `r` refresh, `q` quit
  - Threaded workers for all API calls

### Changed
- `AuthConfig` now stores `bduss` cookie for upload authentication
- `refresh_tokens()` auto-captures `BDUSS` from session cookies
- Login wizard prompts for `BDUSS` cookie alongside `ndus`

### Fixed
- **Upload domain:** Changed from `szb-cdata.1024terabox.com` → `data.1024terabox.com`
  - Old domain returned 403 `"user not exists"` without BDUSS
  - New domain works with just `ndus` cookie
- **Rapid upload bug:** `create` endpoint now always called after precreate,
  even when `block_list` is empty. Previously files would silently not appear
  at the destination path when content already existed in cloud.
- **Rich console in TUI:** Inlined upload logic in `tui.py` to avoid
  `rich.Progress` bar crashes in textual mode
- **Pane refresh after operations:** Fixed `call_from_thread` chaining for
  worker-to-worker refresh calls

---

## [0.1.0] — 2026-06-18

### Added
- Initial release — TeraBox CLI downloader
- `tera auth login` / `tera auth status` — interactive authentication
- `tera ls` — list remote files
- `tera dl` — download from share links or drive paths (multi-worker, resume)
- `tera ul` — upload files with MD5-based rapid upload
- `tera mkdir`, `tera rm`, `tera share`, `tera info`, `tera config`
- `tera-dl` standalone download entry point
- Rich terminal output with progress bars, tables, panels
