---
name: textual-dual-pane-tui
description: Build an MC-style dual-pane file manager TUI (local vs remote) on top of an existing API client using textual, with backend abstraction, threaded workers, and battle-tested ListView/session/error patterns
source: auto-skill
extracted_at: '2026-06-29T01:28:15.512Z'
---

# Dual-Pane TUI on Top of an Existing API Client

When adding an MC-style (Midnight Commander) dual-pane file manager TUI to a project that already has a working API client, use the `textual` framework (built on `rich`, likely already a dependency) with these reusable patterns:

## 1. Verify framework before building

```bash
pip install textual
python -c "import textual; print(textual.__version__)"
```

Confirm it imports cleanly in the target environment before writing any TUI code. Textual works in Termux.

## 2. Backend abstraction layer

Define a `PaneBackend` protocol/interface so local filesystem and remote API share the same UI code path. Both panes call identical methods:

```python
class PaneBackend:
    def list_dir(self, path: str) -> list[FileEntry]: ...
    def mkdir(self, path: str) -> None: ...
    def delete(self, path: str) -> None: ...
    def rename(self, path: str, new_name: str) -> None: ...
    def parent(self, path: str) -> str: ...
    def join(self, base: str, name: str) -> str: ...
    def home(self) -> str: ...
    @property
    def is_local(self) -> bool: ...
```

- `LocalBackend` wraps `pathlib.Path` / `shutil`
- `RemoteBackend` wraps the existing API client methods
- A `FileEntry` dataclass normalizes entries from both sides (`name`, `is_dir`, `size`, `path`)
- Parent path logic differs: local uses `Path.parent`, remote uses string split on `/`

This decouples the UI entirely from the API. Adding a third pane type (SFTP, etc.) requires only a new backend class.

## 3. Threaded workers for blocking API calls

All remote API calls (list, mkdir, delete, rename, upload, download) are blocking. Decorate them with `@work(thread=True)` and push UI updates back via `call_from_thread`:

```python
@work(thread=True)
def _do_refresh(self, pane_name, backend, cwd):
    try:
        entries = backend.list_dir(cwd)
        self.call_from_thread(pane.populate, entries)
    except Exception as e:
        self.call_from_thread(self._status, f"Error: {e}")
```

- Never call API methods directly from the main thread
- Use `call_from_thread` for every UI mutation (status bar, pane populate, refresh)
- Status bar (`Static` widget) doubles as progress indicator during transfers

## 4. Modal screen guards

When a modal dialog (InputDialog for mkdir/rename, ConfirmDialog for delete) is open, app-level keybindings must not fire. Guard every action handler:

```python
def action_mkdir(self):
    if isinstance(self.screen, ModalScreen):
        return
    ...
```

`push_screen(InputDialog(...), callback)` — the callback receives the dialog's `dismiss()` value. `None` means cancelled.

## 5. CLI integration with graceful fallback

Wire the TUI as a subcommand with an ImportError fallback so the dependency stays optional:

```python
@main.command("tui")
@click.pass_context
def tui_cmd(ctx):
    config = get_config(ctx)
    if not config.auth.is_valid:
        console.print("Not authenticated. Run: tera auth login")
        raise click.Abort()
    try:
        from .tui import run_tui
    except ImportError:
        console.print("textual not installed. Run: pip install textual")
        raise click.Abort()
    run_tui(config)
```

- Check auth before launching — TUI can't function without valid credentials
- Lazy import `tui` module so `textual` missing doesn't break the rest of the CLI
- Add `textual>=0.40` to `pyproject.toml` dependencies AND `requirements.txt`

## 6. Keybinding layout (MC convention)

| Key | Action |
|-----|--------|
| Tab | Switch active pane |
| Enter | Open dir / trigger file |
| Backspace | Navigate to parent |
| F2 | Rename |
| F5 | Copy (direction = active→inactive pane) |
| F7 | Mkdir in active pane |
| F8 | Delete (with confirm) |
| r | Refresh |
| q | Quit |

Copy direction: if active pane is local → upload to remote; if active is remote → download to local. The inactive pane's `cwd` is the destination directory.

## 7. File transfer in TUI context — NEVER delegate to CLI functions that use rich.Progress

**Critical failure pattern:** The CLI's `upload_file()` function uses `rich.Progress` bars and `rich.Console.print()`. When called from within a Textual `@work(thread=True)` worker, the `rich.Progress` context manager **crashes silently** — no exception is raised, no error is logged, but the upload does NOT actually complete. The file never appears on remote, yet the status bar says "Upload complete".

This happens because:
- Textual controls stdout/stderr via its own screen manager
- `rich.Progress` writes raw terminal escape sequences to stdout via `Live` display
- The escape sequences corrupt Textual's screen state
- The Progress `__enter__`/`__exit__` lifecycle fails silently, aborting the upload mid-flow

### Symptom: "Upload complete" but file missing

The debugging path that reveals this:
1. Check error log file → **no new errors** (exception was swallowed)
2. List remote directory via API directly → **files not there**
3. Test same upload via standalone CLI script → **CLI works perfectly** (rich Progress works in non-TUI mode)
4. Conclusion: the rich Progress in the delegated function is the culprit

### Fix: inline the upload logic in the TUI

Do NOT call `upload_file()` from the TUI. Instead, inline the 3-step upload (precreate → upload blocks → create) directly in the TUI's `_upload_single` method, using `self.call_from_thread(self._status, ...)` for progress instead of `rich.Progress`:

```python
def _upload_single(self, local_path: str, filename: str, remote_dir: str) -> None:
    """Upload one file — inlined to avoid rich Progress issues in TUI mode."""
    import hashlib
    filepath = Path(local_path)
    file_size = filepath.stat().st_size
    remote_path = f"{remote_dir.rstrip('/')}/{filename}"

    # Compute block hashes
    block_list = []
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            block_list.append(hashlib.md5(chunk).hexdigest())

    # Step 1: Precreate
    data = {"path": remote_path, "size": str(file_size), "autoinit": "1",
            "block_list": json.dumps(block_list), "rtype": "1"}
    resp = self.client.session.post(
        f"{API_DOMAIN}/rest/2.0/xpan/file",
        params=self.client._params({"method": "precreate", "bdstoken": self.client.config.auth.bdstoken}),
        data=data, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if result.get("errno") != 0:
        raise TeraBoxError(f"Precreate errno={result.get('errno')}: {result.get('errmsg', 'unknown')}")

    uploadid = result.get("uploadid", "")
    needed_blocks = result.get("block_list", [])
    # Do NOT return early on empty needed_blocks — see section 12 below

    # Step 2: Upload blocks — use the correct domain (data.1024terabox.com, NOT szb-cdata)
    # Skip this step entirely if needed_blocks is empty (rapid upload — see section 12)
    pcs_url = "https://data.1024terabox.com/rest/2.0/pcs/superfile2"
    total_blocks = len(needed_blocks)
    if total_blocks > 0:
        with open(filepath, "rb") as f:
            for i, block_idx in enumerate(needed_blocks):
                f.seek(int(block_idx) * CHUNK_SIZE)
                chunk = f.read(CHUNK_SIZE)
                files = {"file": ("blob", chunk, "application/octet-stream")}
                params = {"method": "upload", "app_id": "250528", "uploadid": uploadid,
                          "path": remote_path, "partseq": str(block_idx), "uploadsign": "0"}
                resp = self.client.session.post(pcs_url, params=params, files=files, timeout=120)
                resp.raise_for_status()
                pct = (i + 1) * 100 // total_blocks
                self.call_from_thread(self._status,
                    f"Uploading {filename}... {pct}% ({i+1}/{total_blocks} blocks)")

    # Step 3: Create (finalize) — ALWAYS call this, even after rapid upload
    create_data = {"path": remote_path, "size": str(file_size), "uploadid": uploadid,
                   "block_list": json.dumps(block_list), "isdir": "0", "rtype": "1"}
    resp = self.client.session.post(
        f"{API_DOMAIN}/rest/2.0/xpan/file",
        params=self.client._params({"method": "create", "bdstoken": self.client.config.auth.bdstoken}),
        data=create_data, timeout=30)
    resp.raise_for_status()
    final = resp.json()
    if final.get("errno") != 0:
        raise TeraBoxError(f"Create errno={final.get('errno')}: {final.get('errmsg', 'unknown')}")
```

### Redirecting rich Console is NOT sufficient

Attempting to redirect rich Console to StringIO before calling the CLI function does NOT fix the issue — the `rich.Progress` context manager still interferes with Textual's terminal control:

```python
# DOES NOT WORK — Progress still crashes silently
from rich.console import Console
import tera.uploader as ul
ul.console = Console(file=io.StringIO(), force_terminal=False)
result = upload_file(self.client, local_path, remote_path)  # silently fails
```

The only reliable fix is to **not use any rich Progress/Console/Live code path** from within a Textual TUI app. Inline the logic instead.

### Throttle status updates to avoid flooding

Update progress every ~5MB or per-block, not every chunk:

```python
if total and downloaded % (5 * 1024 * 1024) < 65536:
    pct = downloaded * 100 // total
    self.call_from_thread(self._status, f"Downloading {filename}... {pct}%")
```

## 8. Textual 8.x API changes — inspect before writing handlers

Textual's ListView message API changed across major versions. If event handlers silently do nothing (no error, no visible effect), the handler names or message attributes are wrong for the installed version. **Inspect the actual installed API before writing handlers**:

```python
from textual.widgets import ListView
import inspect

# 1. Check what message classes exist (names changed across versions)
for name in dir(ListView):
    if 'select' in name.lower() or 'highlight' in name.lower():
        print(name)

# 2. Check built-in bindings — ListView binds 'enter' to action_select_cursor
print(ListView.BINDINGS)
# [Binding(key='enter', action='select_cursor', ...)]

# 3. Check what attribute holds the source widget on the message
msg = ListView.Selected
print([a for a in dir(msg) if not a.startswith('_')])
```

### Known API differences (textual 8.x)

| Concern | Older textual | Textual 8.x |
|---------|---------------|-------------|
| Select event class | `ListView.ItemSelected` | `ListView.Selected` |
| Highlight event class | `ListView.ItemHighlighted` | `ListView.Highlighted` |
| Handler name (select) | `on_list_view_item_selected` | `on_list_view_selected` |
| Handler name (highlight) | `on_list_view_item_highlighted` | `on_list_view_highlighted` |
| Source widget attr | `event.list_view` | `event.control` |
| Current index | `lv.index` | `lv.index` (still exists) |

Using the old names produces **no error** — the handler simply never matches, so Enter appears dead.

### Enter key being swallowed by ListView

ListView has a built-in `enter` → `action_select_cursor` binding that fires the `Selected` message. Two strategies:

**Strategy A (preferred):** Rely on `on_list_view_selected` instead of `on_key` for Enter. The `Selected` message fires after `action_select_cursor` runs.

**Strategy B:** Intercept in `on_key` with `event.stop()` to prevent ListView's built-in binding from consuming it:

```python
def on_key(self, event):
    if event.key in ("enter", "return"):
        event.stop()           # critical — prevents ListView from swallowing
        self._open_selected()
```

Without `event.stop()`, the app-level `on_key` may run but ListView's binding also fires, causing double-handling or the key being consumed before `on_key` sees it.

### Debugging "Enter does nothing"

When a key press produces zero visible feedback, add status messages at **every** branch to localize the failure:

```python
def _open_selected(self):
    entry = pane.selected_entry()
    if not entry:
        self._status(f"No item selected (active={self._active})")
        return
    if not entry.is_dir:
        self._status(f"{entry.name} is a file, not a folder")
        return
    self._status(f"Opening: {target}")  # confirm we reached here
```

Also add success logging in the refresh worker so you can tell if the API call ran:

```python
self.call_from_thread(self._status, f"{pane_name}: {cwd} ({len(entries)} items)")
```

If neither the "Opening:" nor "Error listing" message appears, the handler isn't being called at all — confirming an API name mismatch rather than a logic bug.

## 9. Remote path fallback

API responses may return an empty `path` field for remote entries. Always construct a fallback path from `cwd + filename`:

```python
file_path = f.get("path", "") or ""
if not file_path:
    file_path = f"{path.rstrip('/')}/{name}"
```

Without this, navigating into a remote folder may silently fail because the target path is empty.

## 10. MC-style multi-select with file marking

True MC workflow requires marking multiple files (not just one highlighted), then acting on the batch. Implement a `marked` set of indices on each pane:

```python
class FilePane(Vertical):
    def __init__(self, ...):
        ...
        self.marked: set[int] = set()  # indices of marked entries

    def _header_text(self) -> str:
        mark = f" ({len(self.marked)} marked)" if self.marked else ""
        return f"{self.title}: {self._cwd}{mark}"

    def _make_item(self, entry: FileEntry) -> ListItem:
        idx = self.entries.index(entry)
        marked = idx in self.marked
        prefix = "[bold yellow]*[/bold yellow] " if marked else "  "
        # ... build text with prefix
```

### Toggle mark + auto-advance cursor

```python
def toggle_mark(self) -> None:
    lv = self.query_one("#file-list", ListView)
    idx = lv.index
    if idx is None or idx < 0 or idx >= len(self.entries):
        return
    if idx in self.marked:
        self.marked.discard(idx)
    else:
        self.marked.add(idx)
    self._redraw()
    if idx + 1 < len(self.entries):  # auto-move down (MC behavior)
        lv.index = idx + 1
```

### Redraw without losing cursor position — use `lv.clear()`, NEVER manual child removal

**Critical crash bug:** Removing ListView children with `for child in list(lv.children): child.remove()` corrupts the internal `_nodes` list. The `lv.index` becomes stale and points beyond the list, causing `IndexError: list index out of range` when the user presses arrow keys after a redraw (e.g., after marking a file). The crash traceback originates in `textual/_list_view.py` → `_loop.py` → `_node_list.py:227`.

**Fix:** Use `lv.clear()` which properly syncs internal state, then clamp `lv.index` to valid range:

```python
def _redraw(self, cursor_idx: int = 0) -> None:
    lv = self.query_one("#file-list", ListView)
    lv.clear()  # NOT: for child in list(lv.children): child.remove()
    for e in self.entries:
        lv.append(self._make_item(e))
    safe_idx = min(cursor_idx, len(self.entries) - 1) if self.entries else None
    lv.index = safe_idx
    self.query_one("#pane-header", Static).update(self._header_text())
```

Same fix applies to `populate()` — use `lv.clear()` and set `lv.index = 0` after appending items.

### Clear marks on navigation

When navigating into a new directory, clear `self.marked` — stale indices from the previous listing would point to wrong files.

### Keybinding: Space AND Insert

In Termux (Android), `Insert` key may not be available. Bind **both** `space` and `insert` for marking so the feature works regardless of terminal:

```python
# In App BINDINGS:
Binding("insert", "toggle_mark", "Mark"),

# In on_key (because Space may not reach BINDINGS cleanly):
elif event.key in ("space", "insert"):
    event.stop()
    self.action_toggle_mark()
```

## 11. Batch transfers with per-file error tracking

**Critical bug pattern:** a batch upload/download loop that catches exceptions per-file but then always prints "Upload complete: N file(s)" — this overwrites individual error messages and falsely reports success even when every file failed.

### Wrong (overwrites errors with false success)

```python
for i, (path, name) in enumerate(...):
    try:
        self._upload_single(path, name, remote_dir)
    except Exception as e:
        self._status(f"Failed: {name}: {e}")  # gets overwritten by next iteration or summary
self._status(f"Upload complete: {total} file(s)")  # ALWAYS says complete
```

### Correct (track successes and failures separately)

```python
@work(thread=True)
def _do_batch_upload(self, paths, names, remote_dir):
    total = len(paths)
    errors = []
    succeeded = 0
    for i, (local_path, filename) in enumerate(zip(paths, names)):
        self.call_from_thread(self._status, f"[{i+1}/{total}] Uploading {filename}...")
        try:
            self._upload_single(local_path, filename, remote_dir)
            succeeded += 1
        except Exception as e:
            err_msg = str(e)
            errors.append(f"{filename}: {err_msg}")
            self.call_from_thread(self._status, f"[red][{i+1}/{total}] Failed: {filename}: {err_msg}[/red]")
    if errors:
        summary = f"Upload: {succeeded} ok, {len(errors)} failed — {errors[0]}"
        self.call_from_thread(self._status, f"[yellow]{summary}[/yellow]")
    else:
        self.call_from_thread(self._status, f"[green]Upload complete: {succeeded}/{total} file(s)[/green]")
    self.call_from_thread(self._refresh_pane, "remote")
```

The summary persists as the last status message, showing the **first error** for quick diagnosis.

## 12. Upload error surfacing — check every API step

TeraBox's chunked upload has 3 steps (precreate → upload blocks → create/finalize). Errors in any step can be silently swallowed if only `resp.raise_for_status()` is checked — the HTTP status is 200 but the JSON body contains `errno != 0`.

### Validate credentials before starting

```python
if not self.client.config.auth.bdstoken:
    raise TeraBoxError("bdstoken is empty — run: tera auth login")
```

An empty bdstoken causes all uploads to fail with confusing errors.

### Check errno in every JSON response, include full response

```python
result = resp.json()
if result.get("errno") != 0:
    raise TeraBoxError(f"Precreate errno={result.get('errno')}: {result.get('errmsg', 'unknown')} (full: {result})")
```

Including `(full: {result})` in the error message lets you see unexpected fields the API returns.

### Check block upload responses too

The `superfile2` upload endpoint also returns JSON that may contain `errno`. Don't assume a 200 status means the block uploaded successfully:

```python
resp = self.client.session.post(pcs_url, params=params, files=files, timeout=120)
resp.raise_for_status()
try:
    upload_result = resp.json()
    if upload_result.get("errno") and upload_result.get("errno") != 0:
        raise TeraBoxError(f"Block upload errno={upload_result.get('errno')}: {upload_result.get('errmsg', 'unknown')}")
except (ValueError, json.JSONDecodeError):
    pass  # Some responses are not JSON
```

### Rapid upload REQUIRES the create step — do NOT return early

When `needed_blocks` is empty after precreate, the file content already exists in the cloud (rapid upload dedup). The server returns `block_list: []` and an `uploadid`. **This is NOT a complete upload.** The file has not been placed at the destination path yet — only the content has been matched.

**Critical bug:** If you `return` early when `needed_blocks` is empty, the TUI reports "Upload complete" but the file never appears in the destination directory. Verified by listing the remote dir via API — only files from prior CLI test uploads are present, not the TUI-uploaded ones.

**The debug log that reveals this:**

```
precreate response: {"block_list":[],"errmsg":"","errno":0,"path":"/testupload/file.jpg",...,"uploadid":"N1-..."}
needed_blocks: []
Rapid upload — file already exists, returning    ← BUG: should continue to create
```

Every file hits rapid upload (because the same images already exist at `/IGO/agatha/`), returns early, and never calls `create`. Result: 0 files actually placed at `/testupload/`.

**Fix:** Skip the block upload loop when `needed_blocks` is empty, but ALWAYS continue to the `create` step:

```python
needed_blocks = result.get("block_list", [])
# Do NOT return here — create is still needed

if needed_blocks:
    # ... upload blocks to pcs_url ...

# ALWAYS call create — this is what places the file at the destination path
create_data = {"path": remote_path, "size": str(file_size), "uploadid": uploadid,
               "block_list": json.dumps(block_list), "isdir": "0", "rtype": "1"}
resp = client.session.post(f"{API_DOMAIN}/rest/2.0/xpan/file",
    params=client._params({"method": "create", "bdstoken": client.config.auth.bdstoken}),
    data=create_data, timeout=30)
```

This bug affects both the TUI (`_upload_single`) and the CLI (`uploader.py` `upload_file`) — fix both code paths.

## 13. Error log file — status bar truncation fallback

The status bar (`Static` widget) is often too narrow to display full error messages (file paths + API error bodies). Users see truncated messages like `Upload: 3 ok, 3 failed. See` with no path visible.

**Pattern:** When a batch operation has failures, write the full errors to a log file and reference it in the status message:

```python
if errors:
    log_path = CONFIG_DIR / "tui_errors.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        from datetime import datetime
        f.write(f"\n=== Upload {datetime.now().isoformat()} ===\n")
        f.write(f"Remote dir: {remote_dir}\n")
        for err in errors:
            f.write(f"  FAIL: {err}\n")
        f.write(f"Succeeded: {succeeded}/{total}\n")
    self.call_from_thread(
        self._status,
        f"[yellow]Upload: {succeeded} ok, {len(errors)} failed. See {log_path}[/yellow]",
    )
```

This gives the user a way to read full errors after quitting the TUI (`cat ~/.config/tera/tui_errors.log`). Without it, errors are lost when the status bar is overwritten by the next message.

## 14. Session cookies and cross-subdomain API calls — the wrong PCS domain

**Bug pattern:** Chunked upload to `szb-cdata.1024terabox.com/rest/2.0/pcs/superfile2` returns `403 Forbidden` with JSON body `{"error_code":31045,"error_msg":"user not exists"}`. Precreate succeeds (errno=0, returns uploadid), but the block upload step fails.

### The 31045 "user not exists" red herring — BDUSS is NOT the answer

Initial hypothesis: the `BDUSS` cookie (used by Baidu Pan internally) is missing from the session and required by the superfile2 endpoint. Exhaustively tested endpoints that do NOT return BDUSS:

| Endpoint | Result |
|----------|--------|
| `GET /main` | Sets `csrfToken`, `browserid` only — no BDUSS |
| `GET /disk/home` | Gzip decode error |
| `GET /passport/get_info` | Returns user info JSON but sets no BDUSS |
| `GET /passport/logininfo` | 404 |
| `GET /passport/transfer` | 404 |
| `GET passport.baidu.com/passapi/loginfo` | HTML page, no BDUSS |
| `GET passport.baidu.com/passapi/domainList` | HTML page, no BDUSS |
| Setting `BDUSS = ndus` value | Still 31045 (ndus ≠ BDUSS) |
| Alternative Baidu PCS domains (`d.pcs.baidu.com`, `c.pcs.baidu.com`) | All return 31045 |

**BDUSS does not exist for TeraBox (international version).** The user confirmed they could not find it in browser DevTools. BDUSS is a Baidu Pan domestic concept; TeraBox uses a different auth model.

### Root cause: wrong PCS upload domain

The `szb-cdata.1024terabox.com` domain rejects TeraBox sessions. The correct domain is `data.1024terabox.com`, which works with the existing `ndus` session cookie — **no additional cookies needed**.

### Discovery process — test multiple domains systematically

When one API endpoint returns auth errors but others work, the issue may be domain-specific. Test candidate domains with a real block upload:

```python
# Precreate succeeds, gives us an uploadid
# Then test which domain accepts the block upload
domains = [
    'https://szb-cdata.1024terabox.com/rest/2.0/pcs/superfile2',  # 403
    'https://data.1024terabox.com/rest/2.0/pcs/superfile2',       # 200 ✓
    'https://c-jp.1024terabox.com/rest/2.0/pcs/superfile2',       # 200 ✓
    'https://ndup.1024terabox.com/rest/2.0/pcs/superfile2',       # DNS fail
    'https://c-sg.1024terabox.com/rest/2.0/pcs/superfile2',       # DNS fail
    'https://d.pcs.baidu.com/rest/2.0/pcs/superfile2',            # 403
    'https://c.pcs.baidu.com/rest/2.0/pcs/superfile2',            # 403
]

for url in domains:
    resp = session.post(url, params=params, files=files, timeout=15)
    print(f'{url}: status={resp.status_code} body={resp.text[:120]}')
```

**How to discover candidate domains:** Fetch the web app's main JS bundle and search for upload-related patterns:

```python
js_url = 'https://s5.teraboxcdn.com/.../js/mainList.3a2a5f89.js'
resp = session.get(js_url, timeout=30)
js = resp.text
# Search for API path patterns
for pattern in ['precreate', 'rapidupload', '/api/create', 'superfile', 'pcs/']:
    matches = re.findall(rf'.{{0,80}}{pattern}.{{0,80}}', js, re.IGNORECASE)
```

The JS revealed that TeraBox web app uses `/api/precreate` + `/api/create` (not `/rest/2.0/xpan/file`), and the block upload goes to `data.` subdomain.

### Fix: use the correct domain

```python
# In uploader.py:
pcs_url = "https://data.1024terabox.com/rest/2.0/pcs/superfile2"
# NOT: "https://szb-cdata.1024terabox.com/rest/2.0/pcs/superfile2"
```

### Key takeaway

When an API endpoint returns auth errors (`31045`, `403`, "user not exists"):
1. **First:** check if other endpoints on the same domain work (if they do, the session is valid)
2. **Second:** test alternative domains for the failing endpoint — the auth model may differ per subdomain
3. **Third:** fetch the official web app's JS bundles to discover the actual endpoints and domains used
4. **Last resort:** look for additional cookies — but verify they actually exist for the service (TeraBox ≠ Baidu Pan domestic)

### Session usage — always use session.post(), never raw requests.post()

When an API client uses `requests.Session` with cookies set for a domain, always use `session.post()` for all requests. Switching to raw `requests.post()` with manually constructed `Cookie` headers drops cookies that were set by prior responses (e.g., `csrfToken`, `browserid` set by `/main` page fetch). The session object is the single source of truth for cookie state.

## 15. Delegate to existing code for debugging — but inline for production

**Two-phase approach:** During debugging, delegate to the CLI's `upload_file()` to eliminate TUI-specific variables. If both CLI and TUI fail identically, the problem is in auth/session/endpoint config, not in the UI layer. Once the root cause is fixed, **inline the logic in the TUI** because CLI functions that use `rich.Progress` crash silently inside Textual (see section 7).

### Phase 1: Debug by delegating

When uploads fail, first test whether the CLI function works in isolation:

```python
# Quick diagnostic script — run outside TUI
from tera.config import Config
from tera.client import TeraBoxClient
from tera.uploader import upload_file

c = Config.load()
client = TeraBoxClient(c)
upload_file(client, test_file, '/testupload/hello.txt')
# If this works, the issue is TUI-specific (likely rich Progress)
# If this fails too, the issue is auth/session/endpoint
```

### Phase 2: Inline for production TUI

After confirming the CLI path works, inline the upload logic in the TUI's `_upload_single` method (see section 7 for full code). Do NOT call `upload_file()` from the TUI — the `rich.Progress` context manager inside it crashes silently.

### Why delegation is useful for debugging but not for production

- **Debugging:** Delegation eliminates code-difference variables. If `tera ul` works from CLI but TUI fails, the problem is in the UI integration layer (rich Progress, worker nesting, etc.)
- **Production:** Delegation fails because `rich.Progress` writes terminal escape sequences to stdout, which Textual controls. The Progress `__enter__`/`__exit__` lifecycle silently aborts. No exception, no error log, no file on remote.

### Debugging strategy when both CLI and TUI fail identically

When delegation reveals that both paths fail the same way, stop looking at code differences and investigate auth/credentials/endpoints:

```python
# Quick diagnostic: print all session cookies and key config values
from tera.config import Config
from tera.client import TeraBoxClient
c = Config.load()
client = TeraBoxClient(c)
print('cookies:', [x.name for x in client.session.cookies])
print('bdstoken:', c.auth.bdstoken[:8] if c.auth.bdstoken else 'MISSING')
```

Then test the actual API call in isolation outside both CLI and TUI to see the raw response.

## 16. @work nested worker bug — pane refresh from inside a worker

**Critical bug pattern:** A `@work(thread=True)` method (e.g., `_do_batch_upload`) finishes and tries to refresh the remote pane by calling `self._refresh_pane("remote")`, which itself calls another `@work(thread=True)` method (`_do_refresh`). **The nested worker does not start.** The pane never refreshes, so newly uploaded files don't appear despite successful upload.

Symptom: "Upload complete: 6/6" shows in status bar, but the remote pane listing doesn't change. User thinks upload failed.

### Root cause

Textual's `@work` decorator creates a worker when called from the main thread/event loop. When called from **inside** an already-running worker thread via `call_from_thread`, the worker registration doesn't happen properly — the method may execute but its `call_from_thread` callbacks to update UI widgets don't fire, or the worker is silently dropped.

### Fix: dispatch refresh back to main thread via call_from_thread

The simplest fix is to use `call_from_thread` to schedule `_refresh_pane` back on the **main thread**, which then spawns the `@work` worker normally:

```python
def _refresh_pane(self, name: str) -> None:
    """Refresh pane from main thread — spawns a worker."""
    self._do_refresh(name)

@work(thread=True)
def _do_refresh(self, pane_name: str) -> None:
    pane = self._get_pane(pane_name)
    entries = pane.backend.list_dir(pane.cwd)
    self.call_from_thread(pane.populate, entries)

def _refresh_pane_from_worker(self, pane_name: str) -> None:
    """Refresh pane when already inside a worker thread.
    Dispatches back to main thread, which spawns the proper @work worker."""
    self.call_from_thread(self._refresh_pane, pane_name)
```

### Dead-end approaches (do NOT use)

1. **Direct `_get_pane` call on worker thread** — `query_one` is not thread-safe; may return stale widget references
2. **Running `list_dir` on main thread via `call_from_thread`** — blocks the UI during the network call
3. **Spawning `@work` from inside `call_from_thread` callback** — the worker may start but its `call_from_thread` callbacks to update widgets don't fire reliably

The only reliable pattern is: `call_from_thread(self._refresh_pane)` → main thread runs `_refresh_pane` → spawns fresh `@work` worker → worker does `list_dir` → `call_from_thread(pane.populate)`.

### Usage in batch workers

```python
@work(thread=True)
def _do_batch_upload(self, paths, names, remote_dir):
    # ... upload loop ...
    # WRONG: self._refresh_pane("remote")  # nested worker, doesn't fire
    # WRONG: self.call_from_thread(self._refresh_pane, "remote")  # seems right but...
    # RIGHT:
    self._refresh_pane_from_worker("remote")
    # which internally does: self.call_from_thread(self._refresh_pane, pane_name)
```

### When this applies

- Any time a `@work` method needs to trigger a pane/list refresh at the end
- Any time one worker needs to "call" another worker's functionality
- Symptom: operation succeeds (confirmed via API), but UI doesn't update

### Verifying uploads actually worked

When "Upload complete" shows but files don't appear in the pane, **always verify via API outside the TUI** before assuming a refresh bug:

```python
# Run this in a separate script to check if files actually exist on remote
from tera.config import Config
from tera.client import TeraBoxClient
c = Config.load()
client = TeraBoxClient(c)
files = client.list_files('/testupload', num=100)
print(f'Files: {len(files)}')
for f in files:
    print(f'  {f.get("server_filename")}  size={f.get("size")}')
```

If files exist on remote but not in pane → refresh bug (see fix above).
If files don't exist on remote either → upload silently failed (see section 7: rich Progress crash).
