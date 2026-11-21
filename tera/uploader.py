import os
import re
import time
import shutil
import hashlib
import json
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress

from .client import TeraBoxClient, TeraBoxError
from .config import Config, API_DOMAIN, HEADERS
from .formatter import progress_columns

console = Console()

CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks for upload


def md5_of_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def compute_block_hashes(filepath: str) -> list[str]:
    """Compute MD5 hash for each 4MB block of a file."""
    hashes = []
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            hashes.append(md5_of_bytes(chunk))
    return hashes


def upload_file(
    client: TeraBoxClient,
    local_path: str,
    remote_path: Optional[str] = None,
    on_progress=None,
) -> dict:
    """Upload a file with chunked upload and rapid upload support.

    on_progress: optional callable(str) receiving status messages. When given,
    no rich progress bar is created (safe inside the TUI).
    """
    def notify(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        else:
            console.print(msg)

    filepath = Path(local_path).resolve()
    if not filepath.exists():
        raise TeraBoxError(f"File not found: {local_path}")

    if remote_path is None:
        remote_path = f"/{filepath.name}"

    file_size = filepath.stat().st_size
    notify(f"[bold]Uploading:[/bold] {filepath.name} ({file_size} bytes)")
    notify(f"[dim]Destination:[/dim] {remote_path}")

    client._ensure_tokens()

    # Compute block hashes
    notify("[dim]Computing file hashes...[/dim]")
    block_list = compute_block_hashes(str(filepath))

    # Step 1: Pre-create (rapid upload check)
    notify("[dim]Checking for rapid upload...[/dim]")
    data = {
        "path": remote_path,
        "size": str(file_size),
        "autoinit": "1",
        "block_list": json.dumps(block_list),
        "rtype": "1",
    }

    result = client._request_json(
        "POST",
        f"{API_DOMAIN}/rest/2.0/xpan/file",
        params=client._params({"method": "precreate", "bdstoken": client.config.auth.bdstoken}),
        data=data,
        timeout=30,
    )

    uploadid = result.get("uploadid", "")
    needed_blocks = result.get("block_list", [])

    if not needed_blocks:
        notify("[dim]Rapid upload — file content already in cloud, finalizing at new path...[/dim]")
    else:
        notify(f"[dim]Need to upload {len(needed_blocks)} block(s)[/dim]")

        # Step 2: Upload chunks
        pcs_url = "https://data.1024terabox.com/rest/2.0/pcs/superfile2"
        total_blocks = len(needed_blocks)

        progress = None
        if on_progress is None:
            progress = Progress(*progress_columns(), console=console)
            progress.start()
            upload_task = progress.add_task("Uploading...", total=file_size)

        try:
            with open(filepath, "rb") as f:
                for i, block_idx in enumerate(needed_blocks, 1):
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

                    client._request_json("POST", pcs_url, params=params, files=files, timeout=120)
                    if progress:
                        progress.update(upload_task, advance=len(chunk))
                    else:
                        pct = i * 100 // total_blocks
                        notify(f"Uploading {filepath.name}... {pct}% ({i}/{total_blocks} blocks)")
        finally:
            if progress:
                progress.stop()

    # Step 3: Create (finalize)
    notify("[dim]Finalizing upload...[/dim]")
    create_data = {
        "path": remote_path,
        "size": str(file_size),
        "uploadid": uploadid,
        "block_list": json.dumps(block_list),
        "isdir": "0",
        "rtype": "1",
    }

    client._request_json(
        "POST",
        f"{API_DOMAIN}/rest/2.0/xpan/file",
        params=client._params({"method": "create", "bdstoken": client.config.auth.bdstoken}),
        data=create_data,
        timeout=30,
    )

    notify("[green]Upload complete![/green]")
    return {}


def _extract_stem(filename: str) -> str:
    """Handle part of a filename, i.e. everything before the timestamp token."""
    base = os.path.splitext(filename)[0]
    m = re.split(r"20\d{2}[_\-]\d{2}[_\-]\d{2}", base, maxsplit=1)
    # strip trailing separators left by the timestamp split (e.g. "xoxoaop_")
    return (m[0] if m else base).strip().rstrip("_").rstrip("-").lower()


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Manual handle -> folder overrides for names the auto-matcher can't infer.
OVERRIDES = {
    "agatha_df": "agatha_chelsea",
    "patria_ao_df": "ptrcia_ao",
    "patria_ao_df1": "ptrcia_ao",
    "nayladumq": "nylaasla",
    "niken_df": "nikendalusi",
    "nikenandalusi": "nikendalusi",
    "danniasalsabilla": "danniasalsabila",
    "kinan_exclu": "Kinandaputriii",
    "kinan_exclu1": "Kinandaputriii",
    "livy4youu": "livyrenata",
    "xoxoaop": "ptrcia_ao",
    "laraskinandaaa": "Kinandaputriii",
    "youknowwhttt": "trslsabila2",
    "nastasiadeline": "natasiadeline",
}


def match_folder(stem: str, folders: list[str]) -> Optional[str]:
    """Resolve a filename stem to a remote folder name, or None to skip."""
    by_norm = {}
    for f in folders:
        fn = _normalize(f)
        if fn:
            by_norm.setdefault(fn, f)

    def resolve(s: str) -> Optional[str]:
        if s in OVERRIDES:
            return OVERRIDES[s]
        n = _normalize(s)
        if not n:
            return None
        if n in by_norm:
            return by_norm[n]
        # Prefix match, but only for folder names >= 4 chars to avoid 'p', 'ayu', etc.
        hits = [f for fn, f in by_norm.items() if len(fn) >= 4 and n.startswith(fn)]
        return hits[0] if len(hits) == 1 else None

    r = resolve(stem)
    if r:
        return r

    # Fallbacks for Instagram dedup/ID suffixes:
    #   youknowwhttt1 -> youknowwhttt
    #   youknowwhttt (1) -> youknowwhttt
    #   youknowwhttt_4022433199002776264 (1) -> youknowwhttt
    for cand in (
        re.sub(r"\s*\(\d+\)$", "", stem),
        re.sub(r"_\d{15,}.*$", "", stem),
        re.sub(r"\d+$", "", stem),
    ):
        if cand and cand != stem:
            r = resolve(cand)
            if r:
                return r
    return None


def sync_local_dir(
    client: TeraBoxClient,
    local_dir: str,
    remote_dir: str,
    uploaded_dir: Optional[str] = None,
    apply: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Upload local files into matching remote subfolders by name, then move them.

    Dry-run (default) prints the plan; `apply=True` performs uploads and moves.
    `limit` caps how many files to upload in this run (for chunked, resumable runs).
    """
    # NOTE: do NOT use Path.resolve() here — on Termux/Python 3.14
    # Path(".").resolve() raises FileNotFoundError via strict realpath
    # when CWD goes through the ~/storage symlink. abspath() raises only
    # when CWD itself vanished, so guard it too.
    try:
        local = Path(os.path.abspath(local_dir))
    except OSError:
        # CWD can vanish (deleted dir / broken storage symlink); abspath then
        # raises FileNotFoundError from os.getcwd(). Fall back to home.
        local = Path(os.path.join(str(Path.home()), local_dir))
    if not local.is_dir():
        raise TeraBoxError(f"Local directory not found: {local_dir} (resolved: {local})")
    remote_folders = [
        f for f in client.list_files(remote_dir, num=1000)
        if int(f.get("isdir") or 0) == 1
    ]
    folder_names = [f.get("server_filename", "unknown") for f in remote_folders]

    files = [
        f for f in os.listdir(local)
        if os.path.isfile(os.path.join(local, f)) and f != ".nomedia"
    ]

    matched = []
    skipped = []
    for name in sorted(files):
        folder = match_folder(_extract_stem(name), folder_names)
        if folder:
            matched.append((name, folder))
        else:
            skipped.append(name)

    console.print(f"\n[bold]Sync plan[/bold] — {len(matched)} to upload, {len(skipped)} to skip")
    console.print(f"Remote root: [cyan]{remote_dir}[/cyan]")
    for name, folder in matched:
        console.print(f"  [green]{name}[/green] -> [cyan]{folder}/[/cyan]")
    for name in skipped:
        console.print(f"  [dim]{name}[/dim] -> [red]skip[/red]")

    if not apply:
        console.print("\n[dim]Dry run — re-run with --apply to upload and move files.[/dim]")
        return {"matched": matched, "skipped": skipped}

    to_upload = matched[:limit] if limit else matched

    uploaded = Path(uploaded_dir) if uploaded_dir else local / "uploaded"
    uploaded.mkdir(parents=True, exist_ok=True)

    ok = fail = 0
    for name, folder in to_upload:
        local_path = local / name
        if not local_path.is_file():
            console.print(f"[red]FAIL {name}: not found at {local_path} (may have been moved/deleted after listing)[/red]")
            fail += 1
            continue
        remote_path = f"{remote_dir.rstrip('/')}/{folder}/{name}"
        for attempt in range(3):
            try:
                upload_file(client, str(local_path), remote_path)
                break
            except Exception as e:
                if attempt == 2:
                    console.print(f"[red]FAIL {name}: {e} [{local_path}][/red]")
                    fail += 1
                else:
                    console.print(f"[yellow]Retry {attempt + 1} {name}: {e}[/yellow]")
                    time.sleep(5)
        else:
            continue
        try:
            shutil.move(str(local_path), str(uploaded / name))
        except FileNotFoundError as e:
            console.print(f"[red]FAIL {name}: move failed — source vanished: {local_path} -> {uploaded / name}: {e}[/red]")
            fail += 1
            continue
        ok += 1

    console.print(f"\n[bold]Done[/bold] — {ok} uploaded, {fail} failed, {len(skipped)} skipped.")
    return {"matched": matched, "skipped": skipped}
