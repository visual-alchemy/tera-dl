from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import requests
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Header, Footer, DataTable, ProgressBar, Static, Input, Button
)
from textual.binding import Binding
from textual import work

from . import __version__
from .client import TeraBoxClient, TeraBoxError
from .config import Config, HEADERS
from .uploader import upload_file
from .formatter import console, format_size, format_size_short, format_timestamp
from .viewer import IMAGE_EXTS


def _short_path(path: str, is_local: bool) -> str:
    """Home-relative path for the pane border title, collapsed when long."""
    home = str(Path.home())
    if is_local and (path == home or path.startswith(home + os.sep)):
        path = "~" + path[len(home):]
    if len(path) > 30:
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) > 2:
            path = "\u2026/" + "/".join(parts[-2:])
    return path


def _age(mtime: int) -> str:
    """Compact age for the date column: now, 5m, 3h, 2d, 11-18, 2025-11-18."""
    if not mtime:
        return ""
    from datetime import datetime as _dt

    then = _dt.fromtimestamp(mtime)
    delta = (_dt.now() - then).total_seconds()
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d"
    if then.year == _dt.now().year:
        return then.strftime("%m-%d")
    return then.strftime("%Y-%m-%d")


def _fit_name(name: str, budget: int) -> str:
    """Middle-truncate keeping the extension visible."""
    if len(name) <= budget:
        return name
    if budget < 6:
        return name[: max(1, budget - 1)] + "\u2026"
    ext = Path(name).suffix
    if ext and len(ext) <= 8 and budget - len(ext) >= 4:
        head = budget - len(ext) - 1
        return name[:head] + "\u2026" + ext
    return name[: budget - 1] + "\u2026"


@dataclass
class FileEntry:
    name: str
    is_dir: bool
    size: int
    path: str
    mtime: int = 0


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
                mtime=int(item.stat().st_mtime),
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
                mtime=int(f.get("server_mtime") or 0),
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


class FileTable(DataTable):
    """DataTable that reports shift+click so the pane can mark a range."""

    shift_handler = None  # callable(row_index) set by FilePane

    async def _on_click(self, event) -> None:
        meta = event.style.meta
        row = meta.get("row")
        if event.shift and self.cursor_type == "row" and row is not None and row >= 0:
            self.cursor_coordinate = (row, 0)
            if self.shift_handler:
                self.shift_handler(row)
            event.stop()
            return
        await super()._on_click(event)


class FilePane(Vertical):
    SORTS = [
        ("name", False), ("name", True),
        ("size", False), ("size", True),
        ("time", False), ("time", True),
    ]

    def __init__(self, title: str, backend: PaneBackend, cwd: str, id: str = None):
        super().__init__(id=id)
        self.title = title
        self.backend = backend
        self._cwd = cwd
        self.entries: list[FileEntry] = []
        self.marked: set[int] = set()  # indices of marked entries
        self._anchor: Optional[int] = None
        self._sort_idx = 0
        self._loading = False

    @property
    def cwd(self) -> str:
        return self._cwd

    def compose(self) -> ComposeResult:
        yield FileTable(id="file-table", cursor_type="row", zebra_stripes=False)

    def on_mount(self) -> None:
        self._table().shift_handler = self.mark_range

    def _table(self) -> FileTable:
        return self.query_one("#file-table", FileTable)

    def _refresh_title(self) -> None:
        path = _short_path(self._cwd, self.backend.is_local)
        self.border_title = f"{self.title} — {path}"
        count = sum(1 for e in self.entries if e.name != "..")
        if self._loading:
            self.border_subtitle = "loading..."
            return
        bits = []
        if count:
            bits.append(f"{count} item{'s' if count != 1 else ''}")
        if self.marked:
            bits.append(f"{len(self.marked)} marked")
        mode, rev = self.SORTS[self._sort_idx]
        bits.append(f"{mode} {'v' if rev else '^'}")
        self.border_subtitle = " · ".join(bits)

    def _show_age(self) -> bool:
        width = self.size.width or max(20, (self.app.size.width or 80) // 2)
        return width >= 44

    def _name_budget(self) -> int:
        width = self.size.width or max(20, (self.app.size.width or 80) // 2)
        return max(8, width - (22 if self._show_age() else 14))

    def _name_cell(self, entry: FileEntry) -> Text:
        name = entry.name + ("/" if entry.is_dir and entry.name != ".." else "")
        name = _fit_name(name, self._name_budget())
        if entry.name == "..":
            return Text(name, style="dim")
        if entry.is_dir:
            return Text(name, style="bold bright_green")
        return Text(name)

    def _row_cells(self, entry: FileEntry, idx: int) -> tuple:
        marker = Text("▌", style="bold yellow") if idx in self.marked else Text(" ")
        size = "" if entry.is_dir else f"{format_size_short(entry.size):>7}"
        cells = (marker, self._name_cell(entry), Text(size))
        if self._show_age():
            cells += (Text(_age(entry.mtime), style="dim"),)
        return cells

    def _columns(self) -> list:
        cols = ["", "Name", "Size"]
        if self._show_age():
            cols.append("Age")
        return cols

    def _rebuild(self, cursor_row: int = 0) -> None:
        table = self._table()
        table.clear(columns=True)
        table.add_columns(*self._columns())
        if not self.entries:
            table.add_row("", Text("(empty)", style="dim"), "", "")
            self._refresh_title()
            return
        for i, entry in enumerate(self.entries):
            table.add_row(*self._row_cells(entry, i), key=str(i))
        if table.row_count:
            table.move_cursor(row=min(cursor_row, table.row_count - 1))
        self._refresh_title()

    def set_loading(self) -> None:
        self.entries = []
        self.marked.clear()
        self._loading = True
        table = self._table()
        table.clear(columns=True)
        table.add_columns(*self._columns())
        self._refresh_title()

    def on_key(self, event) -> None:
        # Focus lives in the DataTable; catch Tab here before Screen's focus_next.
        if event.key == "tab":
            event.stop()
            self.app.action_switch_pane()

    def set_active(self, active: bool) -> None:
        if active:
            self.add_class("-active")
            self._table().focus()
        else:
            self.remove_class("-active")

    def navigate(self, path: str) -> None:
        self._cwd = path
        self.marked.clear()
        self._anchor = None
        self._refresh_title()

    def populate(self, entries: list[FileEntry]) -> None:
        self._loading = False
        self.entries = entries
        self.marked.clear()
        self._anchor = None
        self._sort_entries()
        self._rebuild(0)

    def _sort_entries(self) -> None:
        parent = [e for e in self.entries if e.name == ".."]
        rest = [e for e in self.entries if e.name != ".."]
        mode, rev = self.SORTS[self._sort_idx]
        getter = {
            "name": lambda e: e.name.lower(),
            "size": lambda e: e.size,
            "time": lambda e: e.mtime,
        }[mode]
        rest.sort(key=getter, reverse=rev)
        rest.sort(key=lambda e: not e.is_dir)  # dirs first, regardless of order
        self.entries = parent + rest

    def cycle_sort(self) -> None:
        self._sort_idx = (self._sort_idx + 1) % len(self.SORTS)
        self.marked.clear()
        self._anchor = None
        self._sort_entries()
        self._rebuild(0)

    def toggle_mark(self) -> None:
        idx = self._table().cursor_row
        if idx < 0 or idx >= len(self.entries):
            return
        if idx in self.marked:
            self.marked.discard(idx)
        else:
            self.marked.add(idx)
        self._anchor = idx
        self._rebuild(idx)

    def mark_range(self, target: int) -> None:
        """Mark every row between the anchor and target (shift+click/arrows)."""
        if not self.entries:
            return
        target = max(0, min(target, len(self.entries) - 1))
        anchor = self._anchor if self._anchor is not None else target
        lo, hi = sorted((anchor, target))
        self.marked.update(i for i in range(lo, hi + 1) if self.entries[i].name != "..")
        self._anchor = anchor
        self._rebuild(target)

    def extend_mark(self, delta: int) -> None:
        if not self.entries:
            return
        self.mark_range(self._table().cursor_row + delta)

    def toggle_mark_all(self) -> None:
        all_marked = all(i in self.marked for i in range(len(self.entries)) if self.entries[i].name != "..")
        if all_marked:
            self.marked.clear()
        else:
            self.marked = {i for i in range(len(self.entries)) if self.entries[i].name != ".."}
        self._anchor = None
        cursor = self._table().cursor_row
        self._rebuild(cursor)

    def marked_entries(self) -> list[FileEntry]:
        return [self.entries[i] for i in sorted(self.marked) if i < len(self.entries)]

    def selected_entry(self) -> Optional[FileEntry]:
        idx = self._table().cursor_row
        if idx < 0 or idx >= len(self.entries):
            return None
        return self.entries[idx]


class InputDialog(ModalScreen):
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


class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "close_help", "Close"),
        Binding("question_mark", "close_help", "Close"),
    ]

    HELP = """[bold]tera tui — keys[/bold]

[bold]Tab[/bold]          switch pane
[bold]Enter / →[/bold]    open folder / file
[bold]← / Backspace[/bold] parent folder
[bold]Space / Ins[/bold]  mark / unmark file
[bold]Shift+↑/↓[/bold]    mark range from anchor
[bold]Shift+Click[/bold]  mark range to row
[bold]a[/bold]            mark all / clear all

[bold]F2[/bold]  rename    [bold]F5[/bold]  copy
[bold]F7[/bold]  mkdir     [bold]F8[/bold]  delete
[bold]v[/bold]   view      [bold]r[/bold]   refresh
[bold]s[/bold]   sort      [bold]t[/bold]   theme
[bold][ / ][/bold] pane width  [bold]?[/bold] help
[bold]q[/bold]   quit
"""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.HELP)
            with Horizontal(id="help-buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_close_help(self) -> None:
        self.dismiss()


class TeraBoxTUI(App):
    CSS_PATH = "tui.tcss"
    TITLE = f"tera {__version__}"
    SUB_TITLE = "TeraBox manager"

    BINDINGS = [
        Binding("tab", "switch_pane", "Switch", priority=True),
        Binding("backspace", "parent", "Up"),
        Binding("f2", "rename", "Rename"),
        Binding("f5", "copy", "Copy"),
        Binding("f7", "mkdir", "Mkdir", show=False),
        Binding("f8", "delete", "Delete"),
        Binding("v", "view", "View", show=False),
        Binding("insert", "toggle_mark", "Mark", show=False),
        Binding("a", "select_all", "Select All", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("t", "toggle_theme", "Theme", show=False),
        Binding("left_square_bracket", "pane_narrower", "Narrow", show=False),
        Binding("right_square_bracket", "pane_wider", "Wider", show=False),
        Binding("s", "cycle_sort", "Sort", show=False),
        Binding("shift+up", "extend_mark_up", "Mark up", show=False),
        Binding("shift+down", "extend_mark_down", "Mark down", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, client: TeraBoxClient, config: Config):
        super().__init__()
        self.client = client
        self.config = config
        self._active = "local"
        self._refresh_lock: dict[str, bool] = {}  # debounce per-pane refreshes
        self._ratio = 0.5
        # Handed to run_tui() after the app exits: open_target() kwargs.
        self._pending_view: Optional[dict] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            yield FilePane("Local", LocalBackend(), str(Path.home()), id="local-pane")
            yield FilePane("Remote", RemoteBackend(self.client), "/", id="remote-pane")
        with Horizontal(id="transfer"):
            yield Static("", id="transfer-label")
            yield ProgressBar(id="transfer-bar", total=100)
        with Horizontal(id="status-bar"):
            yield Static("Ready — Tab to switch, Enter to open, F5 to copy", id="status-left")
            yield Static("", id="status-right")
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
        for name in ("local", "remote"):
            self._get_pane(name).set_active(self._active == name)
        try:
            self._update_info(self._active_pane())
        except Exception:
            pass

    def _status(self, msg: str) -> None:
        self.query_one("#status-left", Static).update(msg)

    def _status_right(self, msg: str) -> None:
        self.query_one("#status-right", Static).update(msg)

    def _update_info(self, pane: FilePane) -> None:
        entry = pane.selected_entry()
        marked = f" · {len(pane.marked)} marked" if pane.marked else ""
        if not entry or entry.name == "..":
            self._status_right("")
        elif entry.is_dir:
            self._status_right(f"<DIR>{marked}")
        else:
            when = f" · {format_timestamp(entry.mtime)}" if entry.mtime else ""
            self._status_right(f"{format_size(entry.size)}{when}{marked}")

    def _refresh_pane(self, name: str) -> None:
        """Refresh pane from main thread: show loading, then spawn a worker."""
        if self._refresh_lock.get(name):
            return  # already refreshing this pane
        self._refresh_lock[name] = True
        pane = self._get_pane(name)
        pane.set_loading()
        self._do_refresh(name, pane.backend, pane.cwd)

    @work(thread=True)
    def _do_refresh(self, pane_name: str, backend, cwd: str) -> None:
        try:
            entries = backend.list_dir(cwd)
            if cwd != backend.home():
                entries = [FileEntry(name="..", is_dir=True, size=0, path=backend.parent(cwd))] + entries
            self.call_from_thread(self._finish_refresh, pane_name, entries)
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

    def _finish_refresh(self, pane_name: str, entries: list) -> None:
        pane = self._get_pane(pane_name)
        pane.populate(entries)
        count = sum(1 for e in entries if e.name != "..")
        self._status(f"[green]{pane_name}: {pane.cwd} ({count} items)[/green]")
        self._update_info(pane)

    def _refresh_pane_from_worker(self, pane_name: str) -> None:
        """Refresh pane when already inside a worker thread."""
        self.call_from_thread(self._refresh_pane, pane_name)

    # -- Key handling --

    def on_key(self, event) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if event.key == "left":
            event.stop()
            self.action_parent()
        elif event.key == "right":
            event.stop()
            self._open_selected()
        elif event.key in ("space", "insert"):
            event.stop()
            self.action_toggle_mark()


    def _pane_for_table(self, table) -> Optional[FilePane]:
        for name in ("local", "remote"):
            pane = self._get_pane(name)
            if pane.query_one("#file-table") is table:
                return pane
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        pane = self._pane_for_table(event.data_table)
        if pane is not None:
            self._active = pane.id.removesuffix("-pane")
            self._update_active()
        self._open_selected()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        pane = self._pane_for_table(event.data_table)
        if pane is None:
            return
        name = pane.id.removesuffix("-pane")
        # Ignore the Highlighted event fired by populate(): only follow the
        # cursor when the user actually focused this pane.
        if event.data_table.has_focus and self._active != name:
            self._active = name
            self._update_active()
        if name == self._active:
            self._update_info(pane)

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

    def action_pane_narrower(self) -> None:
        self._set_ratio(self._ratio - 0.1)

    def action_pane_wider(self) -> None:
        self._set_ratio(self._ratio + 0.1)

    def _set_ratio(self, ratio: float) -> None:
        self._ratio = min(0.7, max(0.3, round(ratio, 1)))
        self._get_pane("local").styles.width = f"{int(self._ratio * 100)}%"
        self._get_pane("remote").styles.width = "1fr"
        self._status(f"Local pane: {int(self._ratio * 100)}%")

    def action_extend_mark_up(self) -> None:
        self._active_pane().extend_mark(-1)
        self._update_info(self._active_pane())

    def action_extend_mark_down(self) -> None:
        self._active_pane().extend_mark(1)
        self._update_info(self._active_pane())

    def action_cycle_sort(self) -> None:
        pane = self._active_pane()
        pane.cycle_sort()
        pane.set_active(True)
        self._update_info(pane)
        mode, rev = pane.SORTS[pane._sort_idx]
        self._status(f"Sort: {mode} {'desc' if rev else 'asc'}")

    # -- Transfer panel --

    def _transfer_show(self, label: str) -> None:
        self.query_one("#transfer").add_class("-visible")
        self.query_one("#transfer-label", Static).update(label)
        self.query_one("#transfer-bar", ProgressBar).update(total=100, progress=0)

    def _transfer_update(self, label: str, pct: Optional[float] = None) -> None:
        self.query_one("#transfer-label", Static).update(label)
        if pct is not None:
            self.query_one("#transfer-bar", ProgressBar).update(progress=max(0, min(100, pct)))

    def _transfer_hide(self) -> None:
        self.query_one("#transfer").remove_class("-visible")

    def _upload_progress(self, msg: str) -> None:
        """Runs in the upload worker: hop to the UI thread with clean text."""
        match = re.search(r"(\d+)%", msg)
        label = re.sub(r"\[\/?[a-z ]+\]", "", msg)
        self.call_from_thread(
            self._transfer_update, label, float(match.group(1)) if match else None
        )

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle_theme(self) -> None:
        self.theme = "textual-dark" if self.theme == "textual-light" else "textual-light"
        self._status(f"Theme: {self.theme}")

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
        path = entry.path
        name = entry.name
        ext = os.path.splitext(name)[1].lower()

        try:
            if not backend.is_local:
                self.call_from_thread(self._status, f"Getting link: {name}")
                dlink = self.client.get_download_link(path)
            else:
                dlink = path

            from .config import HEADERS
            from . import viewer

            local = backend.is_local
            # Images go to the system viewer on Android only: desktop openers
            # cannot forward the auth cookie, so mpv is the safer default there.
            external_ok = local or viewer.available() == "termux-open"
            # Deferred: opening a player while the TUI owns the terminal paints
            # over it, so quit first and let run_tui() launch it.
            self._pending_view = {
                "target": dlink,
                "title": name,
                "cookie": "" if local else self.config.auth.cookie_string(),
                "ua": "" if local else HEADERS["User-Agent"],
                "local": local,
                "prefer_external": external_ok and ext in IMAGE_EXTS,
            }
            self.call_from_thread(self.exit)
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
        self.call_from_thread(self._transfer_show, f"Downloading {total} file(s)")
        try:
            for i, (remote_path, filename, size) in enumerate(items):
                self.call_from_thread(
                    self._transfer_update, f"[{i+1}/{total}] {filename}", 0
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
        finally:
            self.call_from_thread(self._transfer_hide)
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
        self.call_from_thread(self._transfer_show, f"Uploading {filename}")
        try:
            upload_file(self.client, local_path, remote_path, on_progress=self._upload_progress)
        finally:
            self.call_from_thread(self._transfer_hide)

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
                            self._transfer_update,
                            f"{filename} — {format_size(downloaded)}/{format_size(total)}",
                            float(pct),
                        )


def run_tui(config: Config) -> None:
    client = TeraBoxClient(config)
    app = TeraBoxTUI(client, config)
    app.run()

    pending = app._pending_view
    if pending:
        from . import viewer

        if not viewer.open_target(**pending):
            console.print("[red]No player found. Install mpv.[/red]")
