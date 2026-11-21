from __future__ import annotations

import os
from datetime import datetime
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
from .config import Config, HEADERS
from .uploader import upload_file
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
        count = sum(1 for e in self.entries if e.name != "..")
        parts = [f"{self.title}: {self._cwd}"]
        if count:
            parts.append(f"({count} item{'s' if count != 1 else ''})")
        if self.marked:
            parts.append(f"{len(self.marked)} marked")
        return " ".join(parts)

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
                text = f"{prefix}[bold bright_green]{entry.name}/[/bold bright_green]"
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

    def toggle_mark_all(self) -> None:
        all_marked = all(i in self.marked for i in range(len(self.entries)) if self.entries[i].name != "..")
        if all_marked:
            self.marked.clear()
        else:
            self.marked = {i for i in range(len(self.entries)) if self.entries[i].name != ".."}
        self._redraw()

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
        Binding("f7", "mkdir", "Mkdir", show=False),
        Binding("f8", "delete", "Delete"),
        Binding("v", "view", "View", show=False),
        Binding("insert", "toggle_mark", "Mark", show=False),
        Binding("a", "select_all", "Select All", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, client: TeraBoxClient, config: Config):
        super().__init__()
        self.client = client
        self.config = config
        self._active = "local"
        self._refresh_lock: dict[str, bool] = {}  # debounce per-pane refreshes
        self._pending_view: Optional[dict] = None  # {dlink, name, cookie, ua, viewer}

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
        import time
        if self._refresh_lock.get(pane_name):
            return  # already refreshing this pane
        self._refresh_lock[pane_name] = True
        try:
            pane = self._get_pane(pane_name)
            backend = pane.backend
            cwd = pane.cwd
            entries = backend.list_dir(cwd)
            if cwd != backend.home():
                entries = [FileEntry(name="..", is_dir=True, size=0, path=backend.parent(cwd))] + entries
            self.call_from_thread(pane.populate, entries)
            self.call_from_thread(self._status, f"[green]{pane_name}: {cwd} ({len(entries)} items)[/green]")
        except TeraBoxError as e:
            err = str(e)
            if "400141" in err or "need verify" in err.lower():
                self.call_from_thread(self._status, "[red]TeraBox rate limit — wait 30s then press r to refresh[/red]")
            else:
                self.call_from_thread(self._status, f"[red]Error listing {pane_name}: {e}[/red]")
        except Exception as e:
            self.call_from_thread(self._status, f"[red]Error listing {pane_name} {cwd}: {e}[/red]")
        finally:
            self._refresh_lock[pane_name] = False

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

    def action_select_all(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        pane = self._active_pane()
        pane.toggle_mark_all()

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

    def action_view(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        pane = self._active_pane()
        entry = pane.selected_entry()
        if not entry or entry.name == "..":
            return
        self._do_view(pane.backend, entry)

    @work(thread=True)
    def _do_view(self, backend: PaneBackend, entry: FileEntry) -> None:
        import shutil

        path = entry.path
        name = entry.name
        ext = os.path.splitext(name)[1].lower()

        try:
            if not backend.is_local:
                self.call_from_thread(self._status, f"Getting link: {name}")
                dlink = self.client.get_download_link(path)
            else:
                dlink = path

            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}:
                viewer = shutil.which("termux-open")
                if viewer:
                    self._pending_view = {"dlink": dlink, "name": name,
                                          "cookie": "", "ua": "",
                                          "viewer": "termux-open"}
                    self.call_from_thread(self.exit)
                    return

            viewer = shutil.which("mpv")
            if viewer:
                from .config import HEADERS
                self._pending_view = {
                    "dlink": dlink, "name": name,
                    "cookie": self.config.auth.cookie_string(),
                    "ua": HEADERS["User-Agent"],
                    "viewer": "mpv",
                }
                self.call_from_thread(self.exit)
                return

            viewer = shutil.which("termux-open")
            if viewer:
                self._pending_view = {"dlink": dlink, "name": name,
                                      "cookie": "", "ua": "",
                                      "viewer": "termux-open"}
                self.call_from_thread(self.exit)
                return

            self.call_from_thread(self._status, "[red]No player. Install mpv: pkg install mpv[/red]")
        except Exception as e:
            self.call_from_thread(self._status, f"[red]View failed: {e}[/red]")

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

        # Gather entries: marked entries take priority, else single highlighted
        entries = pane.marked_entries()
        if not entries:
            entry = pane.selected_entry()
            if not entry or entry.name == "..":
                self._status("[yellow]No file selected — mark files with Insert or highlight one[/yellow]")
                return
            entries = [entry]

        dest_dir = other.cwd

        if pane.backend.is_local:
            self._do_upload_entries(entries, dest_dir)
        else:
            self._do_download_entries(entries, dest_dir)

    @work(thread=True)
    def _do_upload_entries(self, entries: list[FileEntry], remote_dir: str) -> None:
        """Upload entries (files + directories) recursively to remote_dir."""
        import os as _os
        # Flatten: collect all files + directories to create
        dirs_to_create = []   # list of remote paths
        files_to_upload = []  # list of (local_path, remote_path) tuples

        for entry in entries:
            if entry.name == "..":
                continue
            if entry.is_dir:
                # Walk directory recursively
                dir_name = Path(entry.path).name
                remote_base = f"{remote_dir.rstrip('/')}/{dir_name}"
                dirs_to_create.append(remote_base)
                for root, dirs, filenames in _os.walk(entry.path):
                    rel_root = Path(root).relative_to(entry.path)
                    # Create subdirectories
                    for d in dirs:
                        sub_remote = f"{remote_base}/{rel_root / d}".replace("\\", "/")
                        dirs_to_create.append(sub_remote)
                    # Queue files
                    for f in filenames:
                        local_path = _os.path.join(root, f)
                        sub_remote = f"{remote_base}/{rel_root / f}".replace("\\", "/")
                        files_to_upload.append((local_path, sub_remote))
            else:
                remote_path = f"{remote_dir.rstrip('/')}/{entry.name}"
                files_to_upload.append((entry.path, remote_path))

        # Step 1: Create all directories
        total_dirs = len(dirs_to_create)
        for i, d in enumerate(dirs_to_create):
            self.call_from_thread(self._status, f"[{i+1}/{total_dirs}] Creating folder: {d}")
            try:
                self.client.create_directory(d)
            except TeraBoxError as e:
                if "exist" not in str(e).lower():
                    self.call_from_thread(self._status, f"[red]Mkdir failed: {d}: {e}[/red]")

        # Step 2: Upload all files
        total_files = len(files_to_upload)
        errors = []
        succeeded = 0
        for i, (local_path, remote_path) in enumerate(files_to_upload):
            filename = Path(local_path).name
            self.call_from_thread(self._status, f"[{i+1}/{total_files}] Uploading {filename}...")
            try:
                self._upload_single_path(local_path, remote_path)
                succeeded += 1
            except Exception as e:
                errors.append(f"{filename}: {e}")
                self.call_from_thread(self._status, f"[red][{i+1}/{total_files}] Failed: {filename}[/red]")

        if errors:
            from .config import CONFIG_DIR, append_log
            log_path = CONFIG_DIR / "tui_errors.log"
            lines = [f"\n=== Upload {datetime.now().isoformat()} ===", f"Remote dir: {remote_dir}"]
            lines += [f"  FAIL: {err}" for err in errors]
            lines.append(f"Succeeded: {succeeded}/{total_files}")
            append_log(log_path, "\n".join(lines) + "\n", limit=400)
            self.call_from_thread(
                self._status,
                f"[yellow]Upload: {succeeded} ok, {len(errors)} failed. See {log_path}[/yellow]",
            )
        else:
            self.call_from_thread(self._status, f"[green]Upload complete: {succeeded}/{total_files} file(s)[/green]")
        self.call_from_thread(self._refresh_pane, "remote")

    def _upload_single_path(self, local_path: str, remote_path: str) -> None:
        """Upload one file to exact remote path. Wraps _upload_single."""
        parent_dir = str(Path(remote_path).parent)
        self._upload_single(local_path, Path(remote_path).name, parent_dir)

    def _walk_remote(self, path: str, rel_prefix: str, items: list) -> None:
        """Recursively collect (remote_path, rel_name, size) for files under path."""
        for f in self.client.list_files(path, num=1000):
            name = f.get("server_filename", "unknown")
            full = f.get("path", "") or f"{path.rstrip('/')}/{name}"
            rel = os.path.join(rel_prefix, name) if rel_prefix else name
            if int(f.get("isdir", 0)) == 1:
                self._walk_remote(full, rel, items)
            else:
                items.append((full, rel, int(f.get("size", 0))))

    @work(thread=True)
    def _do_download_entries(self, entries: list[FileEntry], dest_dir: str) -> None:
        """Download remote entries (files + directories) to local pane cwd."""
        items = []
        try:
            for e in entries:
                if e.name == "..":
                    continue
                if e.is_dir:
                    self._walk_remote(e.path, e.name, items)
                else:
                    items.append((e.path, e.name, e.size))
        except TeraBoxError as e:
            self.call_from_thread(self._status, f"[red]Listing failed: {e}[/red]")
            return
        if not items:
            self.call_from_thread(self._status, "[yellow]No files in selection[/yellow]")
            return
        self._batch_download_impl(items, dest_dir)

    def _batch_download_impl(self, items: list[tuple[str, str, int]], dest_dir: str) -> None:
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
            from .config import CONFIG_DIR, append_log
            log_path = CONFIG_DIR / "tui_errors.log"
            lines = [f"\n=== Download {datetime.now().isoformat()} ===", f"Dest dir: {dest_dir}"]
            lines += [f"  FAIL: {err}" for err in errors]
            lines.append(f"Succeeded: {succeeded}/{total}")
            append_log(log_path, "\n".join(lines) + "\n", limit=400)
            self.call_from_thread(
                self._status,
                f"[yellow]Download: {succeeded} ok, {len(errors)} failed. See {log_path}[/yellow]",
            )
        else:
            self.call_from_thread(self._status, f"[green]Download complete: {succeeded}/{total} file(s)[/green]")
        self._refresh_pane_from_worker("local")

    def _upload_single(self, local_path: str, filename: str, remote_dir: str) -> None:
        """Upload one file via the shared uploader (callback mode: no rich bar)."""
        remote_path = f"{remote_dir.rstrip('/')}/{filename}"
        upload_file(
            self.client,
            local_path,
            remote_path,
            on_progress=lambda msg: self.call_from_thread(self._status, msg),
        )

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
    import subprocess
    import shutil

    client = TeraBoxClient(config)
    app = TeraBoxTUI(client, config)
    app.run()

    pending = app._pending_view
    if pending:
        dlink = pending["dlink"]
        name = pending["name"]
        if pending["viewer"] == "termux-open":
            subprocess.Popen(
                ["termux-open", "--view", dlink],
                start_new_session=True,
            )
        elif pending["viewer"] == "mpv":
            is_android = shutil.which("termux-open") is not None
            if is_android:
                result = subprocess.run(
                    ["am", "start", "--user", "0",
                     "-a", "android.intent.action.VIEW",
                     "-d", dlink,
                     "-n", "is.xyz.mpv/.MPVActivity"],
                    capture_output=True, timeout=5,
                )
                if result.returncode != 0:
                    subprocess.Popen(
                        ["termux-open", "--view", dlink],
                        start_new_session=True,
                    )
            else:
                headers = f"Cookie: {pending['cookie']}\nUser-Agent: {pending['ua']}"
                subprocess.Popen([
                    "mpv", dlink,
                    f"--force-media-title={name}",
                    f"--http-header-fields={headers}",
                ], start_new_session=True)
