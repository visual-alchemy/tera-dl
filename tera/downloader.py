import os
import asyncio
import aiohttp
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
    TextColumn,
)
from rich.table import Table

from .client import TeraBoxClient, TeraBoxError
from .config import Config, HEADERS

console = Console()


@dataclass
class DownloadTask:
    url: str
    filename: str
    dest_dir: str
    size: int = 0
    status: str = "pending"
    error: str = ""


def format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


async def download_file(
    session: aiohttp.ClientSession,
    url: str,
    dest: Path,
    filename: str,
    size: int,
    progress: Progress,
    task_id,
) -> bool:
    """Download a single file with progress tracking."""
    try:
        headers = {
            "User-Agent": HEADERS["User-Agent"],
            "Cookie": f"ndus=placeholder",  # Will be set by caller
        }

        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            if resp.status != 200:
                progress.update(task_id, description=f"[red]HTTP {resp.status}[/red] {filename}")
                return False

            total = int(resp.headers.get("Content-Length", 0)) or size
            progress.update(task_id, total=total)

            dest_path = dest / filename
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
                    progress.update(task_id, advance=len(chunk))

        return True
    except Exception as e:
        progress.update(task_id, description=f"[red]Error: {e}[/red] {filename}")
        return False


def download_chunk(
    url: str,
    headers: dict,
    start: int,
    end: int,
    part_file_path: Path,
    progress: Progress,
    rich_task_id,
) -> bool:
    """Download a specific byte range of a file."""
    try:
        headers = headers.copy()
        
        # Resume check for this chunk
        already_downloaded = 0
        if part_file_path.exists():
            already_downloaded = part_file_path.stat().st_size
            chunk_limit = end - start + 1
            if already_downloaded > chunk_limit:
                try:
                    part_file_path.unlink()
                except Exception:
                    pass
                already_downloaded = 0
            elif already_downloaded == chunk_limit:
                # Already complete
                return True

        headers["Range"] = f"bytes={start + already_downloaded}-{end}"
        
        import requests as req
        with req.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            # If server ignored Range header and returned full content (200), overwrite instead of append
            is_partial = (r.status_code == 206)
            mode = "ab" if (already_downloaded and is_partial) else "wb"
            with open(part_file_path, mode) as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        progress.update(rich_task_id, advance=len(chunk))
        return True
    except Exception as e:
        console.print(f"[red]Error downloading chunk {start}-{end}: {e}[/red]")
        return False


def merge_chunks(dest_path: Path, part_files: list[Path]) -> bool:
    """Concatenate chunk files and remove temporary parts."""
    try:
        with open(dest_path, "wb") as outfile:
            for part_file in part_files:
                with open(part_file, "rb") as infile:
                    while True:
                        data = infile.read(1024 * 1024)  # 1MB buffer
                        if not data:
                            break
                        outfile.write(data)
        # Delete parts
        for part_file in part_files:
            if part_file.exists():
                part_file.unlink()
        return True
    except Exception as e:
        console.print(f"[red]Error merging chunk files: {e}[/red]")
        return False


def download_file_parallel(
    url: str,
    headers: dict,
    dest_path: Path,
    size: int,
    progress: Progress,
    rich_task_id,
    workers: int = 8,
) -> bool:
    """Coordinate multi-threaded chunk downloading and merging."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    chunk_size = size // workers
    futures = []
    part_files = []
    
    # Calculate already downloaded bytes for progress bar initialization
    already_downloaded_total = 0
    for i in range(workers):
        part_file_path = dest_path.with_name(f"{dest_path.name}.part.{i}")
        part_files.append(part_file_path)
        if part_file_path.exists():
            already_downloaded_total += part_file_path.stat().st_size
            
    if already_downloaded_total > 0:
        progress.update(rich_task_id, completed=already_downloaded_total)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for i in range(workers):
            start = i * chunk_size
            end = size - 1 if i == workers - 1 else (i + 1) * chunk_size - 1
            
            part_file_path = part_files[i]

            future = executor.submit(
                download_chunk,
                url,
                headers,
                start,
                end,
                part_file_path,
                progress,
                rich_task_id
            )
            futures.append(future)

        # Wait for all chunks to complete
        results = [f.result() for f in futures]

    if all(results):
        return merge_chunks(dest_path, part_files)
    else:
        # Do NOT delete partial files on failure to allow resuming later!
        return False



async def download_sequential(
    client: TeraBoxClient,
    tasks: list[DownloadTask],
    dest_dir: Path,
    workers: int = 4,
) -> list[DownloadTask]:
    """Download files sequentially with progress bars."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}[/bold blue]"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        for task in tasks:
            if not task.url:
                try:
                    task.url = client.get_download_link(task.dest_dir + "/" + task.filename)
                except TeraBoxError as e:
                    task.status = "failed"
                    task.error = str(e)
                    console.print(f"[red]Failed to get link for {task.filename}: {e}[/red]")
                    continue

            # Get file size from headers if not already populated
            if not task.size:
                try:
                    import requests
                    head_resp = requests.head(
                        task.url,
                        headers={"User-Agent": "LogStatistic", "Cookie": client.config.auth.cookie_string()},
                        allow_redirects=True,
                        timeout=10,
                    )
                    task.size = int(head_resp.headers.get("Content-Length", 0))
                except Exception:
                    pass

            rich_task = progress.add_task(
                os.path.basename(task.filename),
                total=task.size or None,
            )

            try:
                dest_path = dest_dir / task.filename
                dest_path.parent.mkdir(parents=True, exist_ok=True)

                # Skip check: if file already exists and size matches
                if dest_path.exists() and task.size and dest_path.stat().st_size == task.size:
                    progress.update(rich_task, completed=task.size, description=f"[green]Done (Skipped)[/green] {os.path.basename(task.filename)}")
                    task.status = "done"
                    continue

                headers = {
                    "User-Agent": "LogStatistic",
                    "Cookie": client.config.auth.cookie_string(),
                }

                if task.size and task.size > 1 * 1024 * 1024 and workers > 1:
                    # Multi-threaded segmented download
                    success = download_file_parallel(
                        url=task.url,
                        headers=headers,
                        dest_path=dest_path,
                        size=task.size,
                        progress=progress,
                        rich_task_id=rich_task,
                        workers=workers
                    )
                    if not success:
                        raise Exception("Segmented download failed")
                else:
                    # Single-threaded download fallback
                    import requests as req
                    
                    already_downloaded = 0
                    if dest_path.exists():
                        already_downloaded = dest_path.stat().st_size
                        if task.size and already_downloaded > task.size:
                            already_downloaded = 0

                    if already_downloaded > 0:
                        headers["Range"] = f"bytes={already_downloaded}-{task.size - 1 if task.size else ''}"
                        progress.update(rich_task, completed=already_downloaded)

                    with req.get(task.url, headers=headers, stream=True, timeout=300) as r:
                        r.raise_for_status()
                        is_partial = (r.status_code == 206)
                        
                        total_content_length = int(r.headers.get("Content-Length", 0))
                        total = (total_content_length + already_downloaded) if is_partial else (total_content_length or task.size)
                        if total:
                            progress.update(rich_task, total=total)

                        mode = "ab" if (already_downloaded and is_partial) else "wb"
                        with open(dest_path, mode) as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                                progress.update(rich_task, advance=len(chunk))

                task.status = "done"
                progress.update(rich_task, description=f"[green]Done[/green] {os.path.basename(task.filename)}")

            except Exception as e:
                task.status = "failed"
                task.error = str(e)
                progress.update(rich_task, description=f"[red]Failed[/red] {os.path.basename(task.filename)}: {e}")

    return tasks


def download_files(
    client: TeraBoxClient,
    urls_names_sizes: list[tuple],
    dest_dir: str,
    workers: int = 4,
) -> list[DownloadTask]:
    """
    Download multiple files.
    urls_names_sizes: list of (url, filename) or (url, filename, size) tuples
    """
    dest = Path(dest_dir)
    tasks = []
    for item in urls_names_sizes:
        if len(item) == 3:
            url, name, size = item
        else:
            url, name = item
            size = 0
        tasks.append(DownloadTask(url=url, filename=name, dest_dir=str(dest), size=size or 0))

    console.print(f"\n[bold]Downloading {len(tasks)} file(s) to {dest}[/bold]\n")

    results = asyncio.run(download_sequential(client, tasks, dest, workers))

    # Summary
    done = sum(1 for t in results if t.status == "done")
    failed = sum(1 for t in results if t.status == "failed")

    console.print()
    if failed:
        console.print(f"[yellow]Completed: {done} | Failed: {failed}[/yellow]")
    else:
        console.print(f"[green]All {done} file(s) downloaded successfully![/green]")

    return results


def download_from_share(
    client: TeraBoxClient,
    share_url: str,
    dest_dir: str,
    workers: int = 4,
    pwd: str = "",
) -> list[DownloadTask]:
    """Download all files from a share link recursively."""
    import time
    console.print(f"\n[bold cyan]Resolving share link...[/bold cyan]")

    all_files = []

    def _api_call_with_retry(fn, *args, label="API call", **kwargs):
        """Call an API function with retry on rate limit (400141)."""
        delays = [15, 30, 60, 120, 300]
        for attempt in range(len(delays) + 1):
            try:
                return fn(*args, **kwargs)
            except TeraBoxError as e:
                if "rate_limit" not in str(e).lower() and "400141" not in str(e):
                    raise
                if attempt < len(delays):
                    wait = delays[attempt]
                    console.print(f"[yellow]Rate limit — retrying in {wait}s (attempt {attempt+2}/{len(delays)+1})...[/yellow]")
                    time.sleep(wait)
                    continue
                raise

    def traverse(dir_path: str = "", rel_subfolder: str = ""):
        try:
            items = _api_call_with_retry(
                client.get_share_files, share_url, pwd, dir_path,
                label=f"listing {dir_path or '/'}"
            )
        except TeraBoxError as e:
            console.print(f"[red]Error listing directory {dir_path or '/'}: {e}[/red]")
            return

        for item in items:
            name = item.get("server_filename", "unknown")
            if int(item.get("isdir") or 0) == 1:
                new_rel = os.path.join(rel_subfolder, name) if rel_subfolder else name
                traverse(item.get("path"), new_rel)
            else:
                item["rel_subfolder"] = rel_subfolder
                all_files.append(item)

    traverse()

    if not all_files:
        console.print("[yellow]No files found in share link[/yellow]")
        return []

    # Display files
    table = Table(title="Files found")
    table.add_column("#", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Size", justify="right")

    for i, f in enumerate(all_files, 1):
        name = f.get("server_filename", "unknown")
        rel_sub = f.get("rel_subfolder", "")
        display_name = os.path.join(rel_sub, name) if rel_sub else name
        size = format_size(int(f.get("size") or 0))
        table.add_row(str(i), display_name, size)

    console.print(table)
    console.print()

    # Get download links
    tasks = []
    for f in all_files:
        name = f.get("server_filename", "unknown")
        fs_id = f.get("fs_id", 0)
        size = int(f.get("size") or 0)
        rel_sub = f.get("rel_subfolder", "")

        # Combine relative subfolder with filename to preserve folder hierarchy
        target_name = os.path.join(rel_sub, name) if rel_sub else name

        dlink = f.get("dlink")
        if not dlink:
            try:
                dlink = _api_call_with_retry(
                    client.get_share_dlink, share_url, fs_id, pwd,
                    label=f"dlink for {name}"
                )
            except TeraBoxError as e:
                if "rate_limit" in str(e).lower() or "400141" in str(e):
                    console.print(f"[red]Could not get link for {name}: rate limited[/red]")
                else:
                    console.print(f"[red]Could not get link for {name}: {e}[/red]")
                continue
        tasks.append(DownloadTask(url=dlink, filename=target_name, dest_dir=dest_dir, size=size))

    if not tasks:
        console.print("[red]No download links obtained[/red]")
        return []

    return download_files(client, [(t.url, t.filename, t.size) for t in tasks], dest_dir, workers)


def download_single(
    client: TeraBoxClient,
    source: str,
    dest_dir: str,
    workers: int = 1,
    pwd: str = "",
) -> list[DownloadTask]:
    """Download a single file - auto-detects if it's a share link or drive path."""
    # Check if it's a share URL
    shorturl = TeraBoxClient.parse_share_url(source)
    if shorturl:
        return download_from_share(client, f"https://terabox.com/s/{shorturl}", dest_dir, workers, pwd)

    # Assume it's a drive path
    try:
        url = client.get_download_link(source)
        name = os.path.basename(source)
        size = 0
        try:
            info = client.get_file_info(source)
            size = int(info.get("size") or 0)
        except Exception:
            pass
        return download_files(client, [(url, name, size)], dest_dir, workers)
    except TeraBoxError as e:
        console.print(f"[red]Error: {e}[/red]")
        return []
