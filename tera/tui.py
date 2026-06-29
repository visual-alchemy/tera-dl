from __future__ import annotations

import os
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import requests
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, ListView, ListItem, Static, Label, Input, Button
from textual.binding import Binding
from textual import work

from .client import TeraBoxClient, TeraBoxError
from .config import Config, API_DOMAIN, HEADERS
from .uploader import upload_file, compute_block_hashes, CHUNK_SIZE
from .downloader import download_file
from .formatter import format_size


@dataclass
class FileEntry:
    name: str
    is_dir: bool
    size: int
    path: str


class PaneBackend:
    @property
    def is_local(self) -> bool:
        raise NotImplementedError

    def list_dir(self, path: str) -> list[FileEntry]:
        raise NotImplementedError

    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        raise NotImplementedError

    def rename(self, path: str, new_name: str) -> None:
        raise NotImplementedError

    def parent(self, path: str) -> str:
        raise NotImplementedError

    def join(self, base: str, name: str) -> str:
        raise NotImplementedError

    def home(self) -> str:
        raise NotImplementedError


class LocalBackend(PaneBackend):
    @property
    def is_local(self) -> bool:
        return True

    def list_dir(self, path: str) -> list[FileEntry]:
        p = Path(path)
        entries = []
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.name.startswith("."):
                continue
            entries.append(FileEntry(
                name=item.name,
                is_dir=item.is_dir(),
                size=item.stat().st_size if item.is_file() else 0,
                path=str(item),
            ))
        return entries

    def mkdir(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def delete(self, path: str) -> None:
        import shutil
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()

    def rename(self, path: str, new_name: str) -> None:
        p = Path(path)
        p.rename(p.parent / new_name)

    def parent(self, path: str) -> str:
        return str(Path(path).parent)

    def join(self, base: str, name: str) -> str:
        return str(Path(base) / name)

    def home(self) -> str:
        return str(Path.home())


class RemoteBackend(PaneBackend):
    def __init__(self, client: TeraBoxClient):
        self.client = client

    @property
    def is_local(self) -> bool:
        return False

    def list_dir(self, path: str) -> list[FileEntry]:
        files = self.client.list_files(path, num=1000)
        entries = []
        for f in sorted(files, key=lambda x: (not int(x.get("isdir", 0)), x.get("server_filename", "").lower())):
            name = f.get("server_filename", "unknown")
            file_path = f.get("path", "") or ""
            if not file_path:
                file_path = f"{path.rstrip('/')}/{name}"
            entries.append(FileEntry(
                name=name,
                is_dir=int(f.get("isdir", 0)) == 1,
                size=int(f.get("size", 0)),
                path=file_path,
            ))
        return entries

    def mkdir(self, path: str) -> None:
        self.client.create_directory(path)

    def delete(self, path: str) -> None:
        self.client.delete([path])

    def rename(self, path: str, new_name: str) -> None:
        self.client.rename(path, new_name)

    def parent(self, path: str) -> str:
        if path in ("", "/"):
            return "/"
        parent = "/".join(path.rstrip("/").split("/")[:-1])
        return parent or "/"

    def join(self, base: str, name: str) -> str:
        return f"{base.rstrip('/')}/{name}"

    def home(self) -> str:
        return "/"


class FilePane(Vertical):
    DEFAULT_CSS = """
    FilePane {
        width: 1fr;
        height: 1fr;
        border: solid $panel;
    }
    FilePane.-active {
        border: solid $accent;
    }
    FilePane #pane-header {
        background: $boost;
        padding: 0 1;
        text-style: bold;
    }
    FilePane.-active #pane-header {
        background: $accent 30%;
    }
    """

    def __init__(self, title: str, backend: PaneBackend, cwd: str, id: str = None):
        super().__init__(id=id)
        self.title = title
        self.backend = backend
        self._cwd = cwd
        self.entries: list[FileEntry] = []
        self.marked: set[int] = set()  # indices of marked entries

    @property
    def cwd(self) -> str:
        return self._cwd

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id="pane-header")
        yield ListView(id="file-list")

    def _header_text(self) -> str:
        mark = f" ({len(self.marked)} marked)" if self.marked else ""
        return f"{self.title}: {self._cwd}{mark}"

    def set_active(self, active: bool) -> None:
        if active:
            self.add_class("-active")
            self.query_one("#file-list", ListView).focus()
        else:
            self.remove_class("-active")
        self.query_one("#pane-header", Static).update(self._header_text())

    def navigate(self, path: str) -> None:
        self._cwd = path
        self.marked.clear()
        self.query_one("#pane-header", Static).update(self._header_text())

    def populate(self, entries: list[FileEntry]) -> None:
        self.entries = entries
        self.marked.clear()
        lv = self.query_one("#file-list", ListView)
        lv.clear()
        for e in entries:
            lv.append(self._make_item(e))
        lv.index = 0 if entries else None
        self.query_one("#pane-header", Static).update(self._header_text())

    def _make_item(self, entry: FileEntry) -> ListItem:
        idx = len(self.entries) - 1  # will be correct after append
        # We need index from the entries list
        try:
            idx = self.entries.index(entry)
        except ValueError:
            pass
        marked = idx in self.marked
        prefix = "[bold yellow]*[/bold yellow] " if marked else "  "
        if entry.is_dir:
            if entry.name == "..":
                text = f"{prefix}[dim]../[/dim]"
            else:
                text = f"{prefix}[bold blue]{entry.name}/[/bold blue]"
        else:
            text = f"{prefix}{entry.name}  [dim]{format_size(entry.size)}[/dim]"
        return ListItem(Label(text))

    def toggle_mark(self) -> None:
        lv = self.query_one("#file-list", ListView)
        idx = lv.index
        if idx is None or idx < 0 or idx >= len(self.entries):
            return
        if idx in self.marked:
            self.marked.discard(idx)
        else:
            self.marked.add(idx)
        self._redraw(idx)

    def _redraw(self, cursor_idx: int = 0) -> None:
        lv = self.query_one("#file-list", ListView)
        lv.clear()
        for e in self.entries:
            lv.append(self._make_item(e))
        safe_idx = min(cursor_idx, len(self.entries) - 1) if self.entries else None
        lv.index = safe_idx
        self.query_one("#pane-header", Static).update(self._header_text())

    def marked_entries(self) -> list[FileEntry]:
        return [self.entries[i] for i in sorted(self.marked) if i < len(self.entries)]

    def selected_entry(self) -> Optional[FileEntry]:
        lv = self.query_one("#file-list", ListView)
        idx = lv.index
        if idx is None or idx < 0:
            return None
        if idx < len(self.entries):
            return self.entries[idx]
        return None


class InputDialog(ModalScreen):
    CSS = """
    InputDialog {
        align: center middle;
    }
    InputDialog > Vertical {
        width: 60%;
        height: auto;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    InputDialog Input {
        margin: 1 0;
    }
    InputDialog #dialog-buttons {
        height: auto;
        align-horizontal: right;
    }
    InputDialog Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, initial: str = ""):
        super().__init__()
        self.prompt = prompt
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.prompt)
            yield Input(value=self.initial, id="dialog-input")
            with Horizontal(id="dialog-buttons"):
                yield Button("OK", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#dialog-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#dialog-input", Input).value)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen):
    CSS = """
    ConfirmDialog {
        align: center middle;
    }
    ConfirmDialog > Vertical {
        width: 50%;
        height: auto;
        border: solid $warning;
        padding: 1 2;
        background: $surface;
    }
    ConfirmDialog #confirm-buttons {
        height: auto;
        align-horizontal: center;
    }
    ConfirmDialog Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "No")]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.message)
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", id="yes", variant="error")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class TeraBoxTUI(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #panes {
        height: 1fr;
    }
    #status-bar {
        height: 1;
        background: $boost;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("tab", "switch_pane", "Switch"),
        Binding("backspace", "parent", "Up"),
        Binding("f2", "rename", "Rename"),
        Binding("f5", "copy", "Copy"),
        Binding("f7", "mkdir", "Mkdir"),
        Binding("f8", "delete", "Delete"),
        Binding("insert", "toggle_mark", "Mark"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, client: TeraBoxClient, config: Config):
        super().__init__()
        self.client = client
        self.config = config
        self._active = "local"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            yield FilePane("Local", LocalBackend(), str(Path.home()), id="local-pane")
            yield FilePane("Remote", RemoteBackend(self.client), "/", id="remote-pane")
        yield Static("Ready — Tab to switch, Enter to open, F5 to copy", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_pane("local")
        self._refresh_pane("remote")
        self._update_active()

    # -- Pane management --

    def _get_pane(self, name: str) -> FilePane:
        return self.query_one(f"#{name}-pane", FilePane)

    def _active_pane(self) -> FilePane:
        return self._get_pane(self._active)

    def _inactive_pane(self) -> FilePane:
        other = "remote" if self._active == "local" else "local"
        return self._get_pane(other)

    def _update_active(self) -> None:
        self._get_pane("local").set_active(self._active == "local")
        self._get_pane("remote").set_active(self._active == "remote")

    def _status(self, msg: str) -> None:
        self.query_one("#status-bar", Static).update(msg)

    def _refresh_pane(self, name: str) -> None:
        """Refresh pane from main thread — spawns a worker."""
        self._do_refresh(name)

    @work(thread=True)
    def _do_refresh(self, pane_name: str) -> None:
        try:
            pane = self._get_pane(pane_name)
            backend = pane.backend
            cwd = pane.cwd
            entries = backend.list_dir(cwd)
            if cwd != backend.home():
                entries = [FileEntry(name="..", is_dir=True, size=0, path=backend.parent(cwd))] + entries
            self.call_from_thread(pane.populate, entries)
            self.call_from_thread(self._status, f"[green]{pane_name}: {cwd} ({len(entries)} items)[/green]")
        except Exception as e:
            self.call_from_thread(self._status, f"[red]Error listing {pane_name} {cwd}: {e}[/red]")

    def _refresh_pane_from_worker(self, pane_name: str) -> None:
        """Refresh pane when already inside a worker thread."""
        self.call_from_thread(self._refresh_pane, pane_name)

    # -- Key handling --

    def on_key(self, event) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if event.key in ("enter", "return"):
            event.stop()
            self._open_selected()
        elif event.key == "left":
            event.stop()
            self.action_parent()
        elif event.key == "right":
            event.stop()
            self._open_selected()
        elif event.key in ("space", "insert"):
            event.stop()
            self.action_toggle_mark()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        for name in ("local", "remote"):
            pane = self._get_pane(name)
            if pane.query_one("#file-list") is event.control:
                self._active = name
                self._update_active()
                break
        self._open_selected()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        for name in ("local", "remote"):
            pane = self._get_pane(name)
            if pane.query_one("#file-list") is event.control:
                if self._active != name:
                    self._active = name
                    self._update_active()
                break

    def _open_selected(self) -> None:
        pane = self._active_pane()
        entry = pane.selected_entry()
        if not entry:
            self._status(f"[yellow]No item selected (active={self._active})[/yellow]")
            return
        if not entry.is_dir:
            self._status(f"[yellow]{entry.name} is a file, not a folder[/yellow]")
            return
        if entry.name == "..":
            target = pane.backend.parent(pane.cwd)
        else:
            target = entry.path
        self._status(f"Opening: {target}")
        pane.navigate(target)
        self._refresh_pane(self._active)

    # -- Actions --

    def action_switch_pane(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self._active = "remote" if self._active == "local" else "local"
        self._update_active()

    def action_toggle_mark(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        pane = self._active_pane()
        pane.toggle_mark()

    def action_parent(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        pane = self._active_pane()
        target = pane.backend.parent(pane.cwd)
        pane.navigate(target)
        self._refresh_pane(self._active)

    def action_refresh(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self._refresh_pane(self._active)

    def action_quit(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.exit()

    def action_mkdir(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        pane = self._active_pane()
        backend = pane.backend
        cwd = pane.cwd
        pane_name = self._active

        def _handle(name):
            if not name:
                return
            full_path = backend.join(cwd, name)
            self._do_mkdir(pane_name, backend, full_path)

        self.push_screen(InputDialog("New directory name:"), _handle)

    @work(thread=True)
    def _do_mkdir(self, pane_name: str, backend: PaneBackend, path: str) -> None:
        try:
            self.call_from_thread(self._status, f"Creating: {os.path.basename(path)}")
            backend.mkdir(path)
            self.call_from_thread(self._status, f"[green]Created: {os.path.basename(path)}[/green]")
            self.call_from_thread(self._refresh_pane, pane_name)
        except Exception as e:
            self.call_from_thread(self._status, f"[red]Mkdir failed: {e}[/red]")

    def action_delete(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        pane = self._active_pane()
        pane_name = self._active
        backend = pane.backend

        marked = pane.marked_entries()
        if marked:
            names = [e.name for e in marked]
            count = len(names)
            preview = ", ".join(names[:3])
            if count > 3:
                preview += f" +{count-3} more"
            def _handle(confirmed: bool):
                if confirmed:
                    self._do_batch_delete(pane_name, backend, marked)
            self.push_screen(ConfirmDialog(f"Delete {count} file(s)?\n{preview}"), _handle)
        else:
            entry = pane.selected_entry()
            if not entry or entry.name == "..":
                return
            def _handle(confirmed: bool):
                if confirmed:
                    self._do_delete(pane_name, backend, entry)
            self.push_screen(ConfirmDialog(f"Delete {entry.name}?"), _handle)

    @work(thread=True)
    def _do_delete(self, pane_name: str, backend: PaneBackend, entry: FileEntry) -> None:
        try:
            self.call_from_thread(self._status, f"Deleting: {entry.name}")
            backend.delete(entry.path)
            self.call_from_thread(self._status, f"[green]Deleted: {entry.name}[/green]")
            self.call_from_thread(self._refresh_pane, pane_name)
        except Exception as e:
            self.call_from_thread(self._status, f"[red]Delete failed: {e}[/red]")

    @work(thread=True)
    def _do_batch_delete(self, pane_name: str, backend: PaneBackend, entries: list[FileEntry]) -> None:
        total = len(entries)
        succeeded = 0
        errors = []
        for i, entry in enumerate(entries):
            self.call_from_thread(self._status, f"[{i+1}/{total}] Deleting {entry.name}...")
            try:
                backend.delete(entry.path)
                succeeded += 1
            except Exception as e:
                errors.append(f"{entry.name}: {e}")
                self.call_from_thread(self._status, f"[red][{i+1}/{total}] Failed: {entry.name}[/red]")
        if errors:
            self.call_from_thread(self._status, f"[yellow]Delete: {succeeded} ok, {len(errors)} failed[/yellow]")
        else:
            self.call_from_thread(self._status, f"[green]Deleted: {succeeded}/{total} file(s)[/green]")
        self.call_from_thread(self._refresh_pane, pane_name)

    def action_rename(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        pane = self._active_pane()
        entry = pane.selected_entry()
        if not entry or entry.name == "..":
            return
        pane_name = self._active
        backend = pane.backend

        def _handle(new_name):
            if not new_name:
                return
            self._do_rename(pane_name, backend, entry, new_name)

        self.push_screen(InputDialog("New name:", initial=entry.name), _handle)

    @work(thread=True)
    def _do_rename(self, pane_name: str, backend: PaneBackend, entry: FileEntry, new_name: str) -> None:
        try:
            self.call_from_thread(self._status, f"Renaming: {entry.name} -> {new_name}")
            backend.rename(entry.path, new_name)
            self.call_from_thread(self._status, f"[green]Renamed: {entry.name} -> {new_name}[/green]")
            self.call_from_thread(self._refresh_pane, pane_name)
        except Exception as e:
            self.call_from_thread(self._status, f"[red]Rename failed: {e}[/red]")

    def action_copy(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        pane = self._active_pane()
        other = self._inactive_pane()

        # Gather files: marked entries take priority, else single highlighted
        marked = pane.marked_entries()
        if marked:
            files = [e for e in marked if not e.is_dir and e.name != ".."]
        else:
            entry = pane.selected_entry()
            if not entry or entry.name == "..":
                self._status("[yellow]No file selected — mark files with Insert or highlight one[/yellow]")
                return
            if entry.is_dir:
                self._status("[yellow]Directory copy not supported yet — select files[/yellow]")
                return
            files = [entry]

        if not files:
            self._status("[yellow]No files to copy[/yellow]")
            return

        dest_dir = other.cwd
        total = len(files)

        if pane.backend.is_local:
            self._do_batch_upload([f.path for f in files], [f.name for f in files], dest_dir)
        else:
            self._do_batch_download(
                [(f.path, f.name, f.size) for f in files], dest_dir
            )

    @work(thread=True)
    def _do_batch_upload(self, paths: list[str], names: list[str], remote_dir: str) -> None:
        total = len(paths)
        errors = []
        succeeded = 0
        for i, (local_path, filename) in enumerate(zip(paths, names)):
            self.call_from_thread(
                self._status, f"[{i+1}/{total}] Uploading {filename}..."
            )
            try:
                self._upload_single(local_path, filename, remote_dir)
                succeeded += 1
            except Exception as e:
                err_msg = str(e)
                errors.append(f"{filename}: {err_msg}")
                self.call_from_thread(
                    self._status, f"[red][{i+1}/{total}] Failed: {filename}[/red]"
                )
        if errors:
            from .config import CONFIG_DIR
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
        else:
            self.call_from_thread(self._status, f"[green]Upload complete: {succeeded}/{total} file(s)[/green]")
        self._refresh_pane_from_worker("remote")

    @work(thread=True)
    def _do_batch_download(
        self, items: list[tuple[str, str, int]], dest_dir: str
    ) -> None:
        total = len(items)
        errors = []
        succeeded = 0
        for i, (remote_path, filename, size) in enumerate(items):
            self.call_from_thread(
                self._status, f"[{i+1}/{total}] Downloading {filename}..."
            )
            try:
                self._download_single(remote_path, filename, size, dest_dir)
                succeeded += 1
            except Exception as e:
                err_msg = str(e)
                errors.append(f"{filename}: {err_msg}")
                self.call_from_thread(
                    self._status, f"[red][{i+1}/{total}] Failed: {filename}[/red]"
                )
        if errors:
            from .config import CONFIG_DIR
            log_path = CONFIG_DIR / "tui_errors.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as f:
                from datetime import datetime
                f.write(f"\n=== Download {datetime.now().isoformat()} ===\n")
                f.write(f"Dest dir: {dest_dir}\n")
                for err in errors:
                    f.write(f"  FAIL: {err}\n")
                f.write(f"Succeeded: {succeeded}/{total}\n")
            self.call_from_thread(
                self._status,
                f"[yellow]Download: {succeeded} ok, {len(errors)} failed. See {log_path}[/yellow]",
            )
        else:
            self.call_from_thread(self._status, f"[green]Download complete: {succeeded}/{total} file(s)[/green]")
        self._refresh_pane_from_worker("local")

    def _upload_single(self, local_path: str, filename: str, remote_dir: str) -> None:
        """Upload one file — inlined to avoid rich Progress issues in TUI mode."""
        import hashlib
        from .config import CONFIG_DIR
        log_path = CONFIG_DIR / "tui_upload_debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def dlog(msg):
            with open(log_path, "a") as lf:
                lf.write(f"{msg}\n")
                lf.flush()

        dlog(f"--- Upload start: {filename} ---")
        dlog(f"local_path: {local_path}")
        dlog(f"remote_dir: {remote_dir}")

        filepath = Path(local_path)
        file_size = filepath.stat().st_size
        remote_path = f"{remote_dir.rstrip('/')}/{filename}"
        dlog(f"remote_path: {remote_path}")
        dlog(f"file_size: {file_size}")
        dlog(f"bdstoken: {self.client.config.auth.bdstoken}")
        dlog(f"session cookies: {[(c.name, c.value[:15]) for c in self.client.session.cookies]}")

        # Compute block hashes
        block_list = []
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                block_list.append(hashlib.md5(chunk).hexdigest())
        dlog(f"block_list: {len(block_list)} blocks")

        # Precreate
        data = {
            "path": remote_path,
            "size": str(file_size),
            "autoinit": "1",
            "block_list": json.dumps(block_list),
            "rtype": "1",
        }
        params = self.client._params({"method": "precreate", "bdstoken": self.client.config.auth.bdstoken})
        dlog(f"precreate params: {params}")
        dlog(f"precreate data keys: {list(data.keys())}")
        resp = self.client.session.post(
            f"{API_DOMAIN}/rest/2.0/xpan/file",
            params=params,
            data=data,
            timeout=30,
        )
        dlog(f"precreate status: {resp.status_code}")
        dlog(f"precreate response: {resp.text[:500]}")
        resp.raise_for_status()
        result = resp.json()
        dlog(f"precreate errno: {result.get('errno')}")

        if result.get("errno") != 0:
            dlog(f"ERROR: Precreate failed")
            raise TeraBoxError(f"Precreate errno={result.get('errno')}: {result.get('errmsg', 'unknown')}")

        uploadid = result.get("uploadid", "")
        needed_blocks = result.get("block_list", [])
        dlog(f"uploadid: {uploadid[:40]}")
        dlog(f"needed_blocks: {needed_blocks}")

        if not needed_blocks:
            dlog("Rapid upload — still need to call create to finalize at new path")
        else:
            # Upload blocks
            pcs_url = "https://data.1024terabox.com/rest/2.0/pcs/superfile2"
            total_blocks = len(needed_blocks)

            with open(filepath, "rb") as f:
                for i, block_idx in enumerate(needed_blocks):
                    f.seek(int(block_idx) * CHUNK_SIZE)
                    chunk = f.read(CHUNK_SIZE)

                    files = {"file": ("blob", chunk, "application/octet-stream")}
                    params = {
                        "method": "upload",
                        "app_id": "250528",
                        "uploadid": uploadid,
                        "path": remote_path,
                        "partseq": str(block_idx),
                        "uploadsign": "0",
                    }
                    dlog(f"block {i+1}/{total_blocks}: partseq={block_idx}")
                    resp = self.client.session.post(pcs_url, params=params, files=files, timeout=120)
                    dlog(f"block {i+1} status: {resp.status_code}")
                    dlog(f"block {i+1} response: {resp.text[:300]}")
                    resp.raise_for_status()

                    pct = (i + 1) * 100 // total_blocks
                    self.call_from_thread(
                        self._status,
                        f"Uploading {filename}... {pct}% ({i+1}/{total_blocks} blocks)",
                    )

        # Create (finalize)
        create_data = {
            "path": remote_path,
            "size": str(file_size),
            "uploadid": uploadid,
            "block_list": json.dumps(block_list),
            "isdir": "0",
            "rtype": "1",
        }
        dlog(f"create data: {create_data}")
        resp = self.client.session.post(
            f"{API_DOMAIN}/rest/2.0/xpan/file",
            params=self.client._params({"method": "create", "bdstoken": self.client.config.auth.bdstoken}),
            data=create_data,
            timeout=30,
        )
        dlog(f"create status: {resp.status_code}")
        dlog(f"create response: {resp.text[:500]}")
        resp.raise_for_status()
        final = resp.json()

        if final.get("errno") != 0:
            dlog(f"ERROR: Create failed errno={final.get('errno')}")
            raise TeraBoxError(f"Create errno={final.get('errno')}: {final.get('errmsg', 'unknown')}")

        dlog(f"Upload SUCCESS: {filename}")

    def _download_single(self, remote_path: str, filename: str, size: int, dest_dir: str) -> None:
        """Synchronous download of one file. Called from worker thread."""
        url = self.client.get_download_link(remote_path)

        headers = {
            "User-Agent": "LogStatistic",
            "Cookie": self.client.config.auth.cookie_string(),
        }
        dest_path = Path(dest_dir) / filename
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        with requests.get(url, headers=headers, stream=True, timeout=300) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", size))
            downloaded = 0
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total and downloaded % (5 * 1024 * 1024) < 65536:
                        pct = downloaded * 100 // total
                        self.call_from_thread(
                            self._status,
                            f"Downloading {filename}... {pct}% ({format_size(downloaded)}/{format_size(total)})",
                        )


def run_tui(config: Config) -> None:
    client = TeraBoxClient(config)
    app = TeraBoxTUI(client, config)
    app.run()
