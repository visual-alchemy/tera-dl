# Changelog

All notable changes to tera-dl.

## [Unreleased]

### Added
- `a` keybinding — select all files in active pane (toggle)
- Update README: `pipx` install option, TUI keybinding reference, auth flow docs

### Fixed
- `git safe.directory` issue in Termux environment

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
