import os
import hashlib
import json
import requests
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn

from .client import TeraBoxClient, TeraBoxError
from .config import Config, API_DOMAIN, HEADERS

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
    overwrite: bool = False,
) -> dict:
    """Upload a file to TeraBox with chunked upload and rapid upload support."""
    filepath = Path(local_path).resolve()
    if not filepath.exists():
        raise TeraBoxError(f"File not found: {local_path}")

    if remote_path is None:
        remote_path = f"/{filepath.name}"

    file_size = filepath.stat().st_size
    console.print(f"[bold]Uploading:[/bold] {filepath.name} ({file_size} bytes)")
    console.print(f"[dim]Destination:[/dim] {remote_path}")

    # Compute block hashes
    console.print("[dim]Computing file hashes...[/dim]")
    block_list = compute_block_hashes(str(filepath))

    # Step 1: Pre-create (rapid upload check)
    console.print("[dim]Checking for rapid upload...[/dim]")
    data = {
        "path": remote_path,
        "size": str(file_size),
        "autoinit": "1",
        "block_list": json.dumps(block_list),
        "rtype": "1",
    }

    resp = client.session.post(
        f"{API_DOMAIN}/rest/2.0/xpan/file",
        params=client._params({"method": "precreate", "bdstoken": client.config.auth.bdstoken}),
        data=data,
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()

    if result.get("errno") != 0:
        raise TeraBoxError(f"Precreate failed: {result.get('errmsg', 'unknown')}")

    uploadid = result.get("uploadid", "")
    needed_blocks = result.get("block_list", [])

    if not needed_blocks:
        console.print("[green]Rapid upload succeeded![/green] The file already exists on TeraBox.")
        return result

    console.print(f"[dim]Need to upload {len(needed_blocks)} block(s)[/dim]")

    # Step 2: Upload chunks
    pcs_url = "https://szb-cdata.1024terabox.com/rest/2.0/pcs/superfile2"

    with Progress(
        TextColumn("[bold blue]{task.description}[/bold blue]"),
        BarColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        upload_task = progress.add_task("Uploading...", total=len(needed_blocks))

        with open(filepath, "rb") as f:
            for block_idx in needed_blocks:
                f.seek(block_idx * CHUNK_SIZE)
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

                resp = client.session.post(
                    pcs_url,
                    params=params,
                    files=files,
                    timeout=120,
                )
                resp.raise_for_status()
                progress.update(upload_task, advance=1)

    # Step 3: Create (finalize)
    console.print("[dim]Finalizing upload...[/dim]")
    create_data = {
        "path": remote_path,
        "size": str(file_size),
        "uploadid": uploadid,
        "block_list": json.dumps(block_list),
        "isdir": "0",
        "rtype": "1",
    }

    resp = client.session.post(
        f"{API_DOMAIN}/rest/2.0/xpan/file",
        params=client._params({"method": "create", "bdstoken": client.config.auth.bdstoken}),
        data=create_data,
        timeout=30,
    )
    resp.raise_for_status()
    final = resp.json()

    if final.get("errno") != 0:
        raise TeraBoxError(f"Create failed: {final.get('errmsg', 'unknown')}")

    console.print("[green]Upload complete![/green]")
    return final
