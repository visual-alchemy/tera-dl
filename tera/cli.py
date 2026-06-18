import click
import os
from pathlib import Path

from . import __version__
from .config import Config
from .auth import login_interactive, verify_auth, AuthError
from .client import TeraBoxClient, TeraBoxError
from .downloader import download_single, download_from_share, download_files
from .uploader import upload_file
from .formatter import (
    console,
    print_file_list,
    print_user_info,
    print_download_summary,
    print_share_info,
    format_size,
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


@main.command("dl")
@click.argument("sources", nargs=-1, required=True)
@click.option("-o", "--output", default=None, help="Output directory")
@click.option("-w", "--workers", default=None, type=int, help="Parallel download workers")
@click.option("-p", "--pwd", default="", help="Password for protected share links")
@click.pass_context
def download(ctx, sources, output, workers, pwd):
    """Download from share links or drive paths.

    SOURCES can be TeraBox share URLs (https://terabox.com/s/xxxxx)
    or file paths in your drive (/MyFolder/video.mp4).
    """
    config = get_config(ctx)
    client = get_client(ctx)

    dest = output or config.download_dir
    w = workers or config.workers

    for source in sources:
        results = download_single(client, source, dest, workers=w, pwd=pwd)
        if results:
            print_download_summary(results)


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
    client = get_client(ctx)
    try:
        quota = client.get_quota()
        used = quota.get("used", 0)
        total = quota.get("total", 0)
        console.print(f"\n[bold]Storage:[/bold] {format_size(used)} / {format_size(total)}")
    except TeraBoxError as e:
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


@click.command()
@click.argument("sources", nargs=-1, required=True)
@click.option("-o", "--output", default=None, help="Output directory")
@click.option("-w", "--workers", default=None, type=int, help="Parallel download workers")
@click.option("-p", "--pwd", default="", help="Password for protected share links")
@click.pass_context
def download_standalone(ctx, sources, output, workers, pwd):
    """Download from share links or drive paths directly."""
    ctx.ensure_object(dict)
    config = Config.load()
    client = TeraBoxClient(config)

    dest = output or config.download_dir
    w = workers or config.workers

    for source in sources:
        results = download_single(client, source, dest, workers=w, pwd=pwd)
        if results:
            print_download_summary(results)


if __name__ == "__main__":
    main()
