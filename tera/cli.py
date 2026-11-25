import click
import os

from . import __version__
from .config import Config
from .auth import login_interactive, verify_auth, AuthError
from .client import TeraBoxClient, TeraBoxError
from .downloader import download_single, download_files
from .uploader import upload_file, sync_local_dir
from .formatter import (
    console,
    print_file_list,
    print_user_info,
    print_share_info,
)


def get_config(ctx) -> Config:
    if "config" not in ctx.obj:
        ctx.obj["config"] = Config.load()
    return ctx.obj["config"]


def get_client(ctx) -> TeraBoxClient:
    config = get_config(ctx)
    return TeraBoxClient(config)


@click.group()
@click.version_option(__version__, prog_name="tera")
@click.pass_context
def main(ctx):
    """TeraBox CLI - Bypass download/upload limitations."""
    ctx.ensure_object(dict)


# ── Auth commands ──────────────────────────────────────────────────────


@main.group()
def auth():
    """Authentication management."""
    pass


@auth.command("login")
@click.pass_context
def auth_login(ctx):
    """Login with TeraBox cookies."""
    config = get_config(ctx)
    try:
        config = login_interactive(config)
        ctx.obj["config"] = config

        # Verify
        info = verify_auth(config)
        console.print(f"\n[green]Logged in as: {info.get('uname', 'unknown')}[/green]")
    except AuthError as e:
        console.print(f"[red]Login failed: {e}[/red]")
        raise click.Abort()


@auth.command("status")
@click.pass_context
def auth_status(ctx):
    """Check authentication status."""
    config = get_config(ctx)
    try:
        info = verify_auth(config)
        print_user_info(info)
    except AuthError as e:
        console.print(f"[red]Not authenticated: {e}[/red]")
        raise click.Abort()


# ── File commands ──────────────────────────────────────────────────────


@main.command("ls")
@click.argument("path", default="/")
@click.option("-n", "--num", default=100, help="Max items to list")
@click.pass_context
def list_files(ctx, path, num):
    """List files in a directory."""
    client = get_client(ctx)
    try:
        files = client.list_files(path, num=num)
        if not files:
            console.print(f"[dim]No files in {path}[/dim]")
        else:
            print_file_list(files, path)
    except TeraBoxError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


def download_sources(
    client: TeraBoxClient, sources, dest: str, workers: int, pwd: str, retries: int = 1
) -> list:
    """Download every source, then re-run only the sources that had failures.

    Files already complete on disk are skipped by size, so a retry pass only
    transfers what is missing. Returns the sources that still failed.
    """
    import time

    pending = list(sources)
    for attempt in range(retries + 1):
        if attempt:
            console.print(
                f"\n[yellow]Retry {attempt}/{retries}: {len(pending)} source(s) "
                f"with earlier failures[/yellow]"
            )
            time.sleep(2.0 * attempt)  # back off; TeraBox throttles fast retries
        again = []
        total = len(pending)
        for i, source in enumerate(pending, 1):
            tag = f"[link {i}/{total}] " if total > 1 else ""
            console.print(f"\n[bold cyan]{tag}{source}[/bold cyan]")
            if i > 1 and TeraBoxClient.parse_share_url(source):
                time.sleep(1.0)  # throttle between share links to avoid rate limit
            results = download_single(client, source, dest, workers=workers, pwd=pwd)
            # ponytail: an empty result set (link resolution failed) also counts
            # as a failure, so it gets the retry pass; upgrade if empty folders
            # ever need to be distinguished from errors.
            if any(r.status == "failed" for r in results) or not results:
                again.append(source)
        pending = again
        if not pending:
            break
    return pending


@main.command("dl")
@click.argument("sources", nargs=-1, required=True)
@click.option("-o", "--output", default=None, help="Output directory")
@click.option("-w", "--workers", default=None, type=int, help="Parallel download workers")
@click.option("-p", "--pwd", default="", help="Password for protected share links")
@click.option("--retries", default=1, type=int, help="Retry passes for failed sources")
@click.pass_context
def download(ctx, sources, output, workers, pwd, retries):
    """Download from share links or drive paths.

    SOURCES can be TeraBox share URLs (https://terabox.com/s/xxxxx)
    or file paths in your drive (/MyFolder/video.mp4).
    """
    config = get_config(ctx)
    client = get_client(ctx)

    dest = output or config.download_dir
    w = workers or config.workers

    failed = download_sources(client, sources, dest, w, pwd, retries)
    if failed:
        console.print(f"\n[red]{len(failed)} source(s) still failing after retries.[/red]")
        raise SystemExit(1)


@main.command("ul")
@click.argument("local_path")
@click.option("-r", "--remote", default=None, help="Remote path (default: /filename)")
@click.pass_context
def upload(ctx, local_path, remote):
    """Upload a file to TeraBox."""
    client = get_client(ctx)
    try:
        upload_file(client, local_path, remote)
    except TeraBoxError as e:
        console.print(f"[red]Upload failed: {e}[/red]")
        raise click.Abort()


@main.command("sync")
@click.argument("local_dir")
@click.argument("remote_dir")
@click.option("--apply", is_flag=True, help="Actually upload and move files (default: dry-run preview)")
@click.option("--uploaded-dir", default=None, help="Move uploaded files here (default: <local_dir>/uploaded)")
@click.option("--limit", default=None, type=int, help="Upload at most N files this run")
@click.pass_context
def sync(ctx, local_dir, remote_dir, apply, uploaded_dir, limit):
    """Upload local files into matching remote subfolders by name, then move them."""
    client = get_client(ctx)
    try:
        sync_local_dir(client, local_dir, remote_dir, uploaded_dir, apply=apply, limit=limit)
    except TeraBoxError as e:
        console.print(f"[red]Sync failed: {e}[/red]")
        raise click.Abort()


@main.command("rm")
@click.argument("path")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.pass_context
def delete(ctx, path, yes):
    """Delete a file or directory."""
    client = get_client(ctx)

    if not yes:
        if not click.confirm(f"Delete {path}?"):
            return

    try:
        client.delete([path])
        console.print(f"[green]Deleted: {path}[/green]")
    except TeraBoxError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@main.command("mkdir")
@click.argument("path")
@click.pass_context
def make_dir(ctx, path):
    """Create a directory."""
    client = get_client(ctx)
    try:
        client.create_directory(path)
        console.print(f"[green]Created directory: {path}[/green]")
    except TeraBoxError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@main.command("share")
@click.argument("path")
@click.option("--period", default=0, type=int, help="Expiry period in days (0=permanent)")
@click.pass_context
def create_share(ctx, path, period):
    """Create a share link for a file."""
    client = get_client(ctx)
    try:
        info = client.get_file_info(path)
        fs_id = info.get("fs_id")
        if not fs_id:
            console.print("[red]Could not get file ID[/red]")
            raise click.Abort()

        result = client.create_share([fs_id], period=period)
        print_share_info(result)
    except TeraBoxError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@main.command("info")
@click.pass_context
def account_info(ctx):
    """Show account info and storage usage."""
    config = get_config(ctx)
    client = get_client(ctx)
    try:
        client._ensure_tokens()
        print_user_info(verify_auth(config))
    except (TeraBoxError, AuthError) as e:
        console.print(f"[red]Error: {e}[/red]")


@main.command("config")
@click.option("--set-download-dir", help="Set default download directory")
@click.option("--set-workers", type=int, help="Set default parallel workers")
@click.pass_context
def config_cmd(ctx, set_download_dir, set_workers):
    """View or modify configuration."""
    config = get_config(ctx)

    if set_download_dir:
        config.download_dir = set_download_dir
        config.save()
        console.print(f"[green]Download directory set to: {set_download_dir}[/green]")

    if set_workers:
        config.workers = set_workers
        config.save()
        console.print(f"[green]Workers set to: {set_workers}[/green]")

    if not set_download_dir and not set_workers:
        console.print(f"[bold]Current configuration:[/bold]")
        console.print(f"  Download dir: {config.download_dir}")
        console.print(f"  Workers: {config.workers}")
        console.print(f"  Auth: {'Configured' if config.auth.is_valid else 'Not configured'}")


@main.command("view")
@click.argument("path")
@click.pass_context
def view_file(ctx, path):
    """Stream a file from TeraBox to your video player.

    Gets a direct download link and opens it in an external player.
    On Android/Termux: downloads to temp, then opens it.
    On desktop: opens in mpv (or the system opener) with auth headers.
    """
    import shutil

    from .config import HEADERS
    config = get_config(ctx)
    client = get_client(ctx)
    try:
        console.print(f"[dim]Getting download link...[/dim]")
        dlink = client.get_download_link(path)
        if not dlink:
            console.print("[red]Could not get download link[/red]")
            raise click.Abort()
    except TeraBoxError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()

    name = path.rstrip("/").split("/")[-1]
    cookie = config.auth.cookie_string()
    ua = HEADERS["User-Agent"]

    if shutil.which("termux-open"):
        _view_android(client, dlink, name, cookie)
    else:
        _view_desktop(dlink, name, cookie, ua)


def _view_android(client, dlink: str, name: str, cookie: str) -> None:
    """Download to temp, then open it (avoids slow CDN streaming)."""
    import glob as _glob
    import tempfile
    import time as _time
    import shutil as _shutil

    from . import viewer
    from .downloader import download_files

    tmpdir = tempfile.gettempdir()
    # Cleanup temp dirs older than 1 hour
    cutoff = _time.time() - 3600
    for old in _glob.glob(os.path.join(tmpdir, "teraview_*")):
        try:
            if os.path.getmtime(old) < cutoff:
                _shutil.rmtree(old, ignore_errors=True)
        except Exception:
            pass

    dest = tempfile.mkdtemp(prefix="teraview_", dir=tmpdir)

    console.print(f"[dim]Downloading {name} to temp...[/dim]")
    try:
        results = download_files(client=client, urls_names_sizes=[(dlink, name, 0)],
                                 dest_dir=dest, workers=4)
    except Exception:
        results = []

    if results and results[0].status == "done":
        local_path = os.path.join(dest, name)
        used = viewer.open_target(local_path, local=True)
        if used:
            console.print(f"[green]Opening: {name}[/green]")
            return
        console.print(f"[red]No player found. Install mpv: pkg install mpv[/red]")
        return

    console.print("[yellow]Download skipped, streaming directly[/yellow]")
    used = viewer.open_target(dlink, title=name, cookie=cookie)
    if used:
        console.print(f"[green]Streaming: {name}[/green]")
    else:
        console.print(f"[red]No player found. Install mpv: pkg install mpv[/red]")


def _view_desktop(dlink: str, name: str, cookie: str, ua: str) -> None:
    """Open dlink in a local player (mpv preferred)."""
    from . import viewer

    used = viewer.open_target(dlink, title=name, cookie=cookie, ua=ua)
    if not used:
        console.print("[red]No player found. Install mpv.[/red]")
        return
    console.print(f"[green]Streaming in {used}: {name}[/green]")
    if used in ("xdg-open", "open"):
        # Those cannot forward the cookie, so TeraBox may refuse the stream.
        console.print("[yellow]Install mpv to stream authenticated links.[/yellow]")


@main.command("tui")
@click.pass_context
def tui_cmd(ctx):
    """Launch MC-style dual-pane file manager."""
    config = get_config(ctx)
    if not config.auth.is_valid:
        console.print("[red]Not authenticated. Run: tera auth login[/red]")
        raise click.Abort()
    try:
        from .tui import run_tui
    except ImportError:
        console.print("[red]textual not installed. Run: pip install textual[/red]")
        raise click.Abort()
    run_tui(config)


@click.command()
@click.argument("sources", nargs=-1, required=True)
@click.option("-o", "--output", default=None, help="Output directory")
@click.option("-w", "--workers", default=None, type=int, help="Parallel download workers")
@click.option("-p", "--pwd", default="", help="Password for protected share links")
@click.option("--retries", default=1, type=int, help="Retry passes for failed sources")
@click.pass_context
def download_standalone(ctx, sources, output, workers, pwd, retries):
    """Download from share links or drive paths directly."""
    ctx.ensure_object(dict)
    config = Config.load()
    client = TeraBoxClient(config)

    dest = output or config.download_dir
    w = workers or config.workers

    failed = download_sources(client, sources, dest, w, pwd, retries)
    if failed:
        console.print(f"\n[red]{len(failed)} source(s) still failing after retries.[/red]")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
