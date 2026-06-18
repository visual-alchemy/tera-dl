from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

console = Console()


def format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_timestamp(ts: int) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "unknown"


def print_file_list(files: list[dict], path: str = "/"):
    """Print files in a nice table."""
    table = Table(title=f"Files in {path}")
    table.add_column("Type", style="dim", width=5)
    table.add_column("Name", style="bold")
    table.add_column("Size", justify="right")
    table.add_column("Modified", justify="right")

    for f in sorted(files, key=lambda x: (not x.get("isdir", 0), x.get("server_filename", ""))):
        is_dir = f.get("isdir", 0) == 1
        name = f.get("server_filename", "unknown")
        size = format_size(int(f.get("size") or 0)) if not is_dir else "-"
        modified = format_timestamp(f.get("server_mtime", 0))
        type_str = "[blue]DIR[/blue]" if is_dir else "FILE"
        name_str = f"[blue]{name}/[/blue]" if is_dir else name

        table.add_row(type_str, name_str, size, modified)

    console.print(table)


def print_user_info(info: dict):
    """Print user account info."""
    uname = info.get("uname", "unknown")
    used = info.get("used", 0)
    total = info.get("total", 0)

    used_str = format_size(used)
    total_str = format_size(total)
    percent = (used / total * 100) if total else 0

    panel_text = (
        f"[bold]User:[/bold] {uname}\n"
        f"[bold]Storage:[/bold] {used_str} / {total_str} ({percent:.1f}%)"
    )

    console.print(Panel(panel_text, title="TeraBox Account", border_style="cyan"))


def print_download_summary(results: list):
    """Print download results summary."""
    done = sum(1 for r in results if r.status == "done")
    failed = sum(1 for r in results if r.status == "failed")

    if failed:
        console.print(f"\n[yellow]Downloads: {done} completed, {failed} failed[/yellow]")
        for r in results:
            if r.status == "failed":
                console.print(f"  [red]x[/red] {r.filename}: {r.error}")
    else:
        console.print(f"\n[green]All {done} download(s) completed successfully![/green]")


def print_share_info(share_data: dict):
    """Print share link info."""
    shorturl = share_data.get("shorturl", "")
    link = share_data.get("link", "")

    if shorturl:
        url = f"https://terabox.com/s/{shorturl}"
    elif link:
        url = link
    else:
        url = "unknown"

    console.print(f"\n[green]Share link created:[/green] {url}")
