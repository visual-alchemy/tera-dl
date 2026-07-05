---
name: AGENTS
description: AI agent instructions for the tera-dl project
---

# tera-dl — Agent Guide

TeraBox CLI + TUI for bypassing download/upload limitations on TeraBox cloud storage.

## Architecture

```
tera/
├── __init__.py          # Version (0.1.0)
├── cli.py               # Click CLI: auth, ls, dl, ul, rm, mkdir, share, info, config, tui
├── config.py            # Config/AuthConfig dataclasses, API constants, persistence
├── auth.py              # Interactive login, token extraction from page HTML
├── client.py            # TeraBoxClient — HTTP API wrapper (list, upload, download, share, etc.)
├── downloader.py        # Multi-threaded segmented download with resume
├── uploader.py          # Chunked upload with MD5-based rapid upload dedup
├── formatter.py         # Rich terminal output formatting (tables, panels)
├── tui.py               # Textual dual-pane file manager (local vs remote)
```

## API Layer

- **Domain:** `https://1024terabox.com` (main API), `https://data.1024terabox.com` (upload blocks)
- **Auth:** `ndus` cookie + auto-extracted `jsToken` + `bdstoken` from `/main` page HTML
- **Upload flow:** precreate → upload blocks to `data.1024terabox.com/rest/2.0/pcs/superfile2` → create (finalize)
- **Key params:** `app_id=250528`, `channel=dubox`, `web=1`

## TUI (textual framework)

- `tera tui` launches dual-pane: local (`LocalBackend`) + remote (`RemoteBackend`)
- All API calls run in `@work(thread=True)` workers — never block the UI
- UI updates go through `self.call_from_thread(...)` to stay on main thread

## Critical Lessons

1. **Upload domain:** Must use `data.1024terabox.com`, NOT `szb-cdata.1024terabox.com`
   - `szb-cdata` returns 403 `"user not exists"` without BDUSS cookie
   - `data.1024terabox.com` works with just `ndus` cookie

2. **Rapid upload needs `create`:** When precreate returns `block_list: []` (rapid upload),
   still call the `create` endpoint to finalize the file at the destination path.
   Returning early without `create` leaves no file.

3. **Rich console in TUI:** `rich.Progress` bars crash silently in textual mode.
   Upload logic in `tui.py` is inlined — not delegated to `uploader.py`.

4. **Thread safety:** `_get_pane()` uses `query_one` which must be on main thread.
   Workers must use `call_from_thread` to schedule UI operations.

5. **Batch operations** (copy/delete): If files are marked, operate on marked set.
   If none marked, operate on the single highlighted entry.

## Commands

```bash
tera tui              # Interactive dual-pane file manager
tera auth login       # Set up authentication
tera ls /path         # List remote files
tera dl <url|path>    # Download from share link or drive path
tera ul <file>        # Upload local file
tera mkdir /path      # Create remote directory
tera rm /path -y      # Delete remote file/directory
tera info             # Storage usage
```

## Dependencies

`click`, `requests`, `rich`, `aiohttp`, `textual` — all declared in `pyproject.toml`.
