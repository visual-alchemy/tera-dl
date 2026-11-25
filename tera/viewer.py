"""Pick an external player/opener. Shared by the CLI and the TUI.

Order: termux-open (Android) -> mpv -> open (macOS) / xdg-open (Linux).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}

# Players we know how to drive, best first.
CANDIDATES = ("termux-open", "mpv", "open", "xdg-open")


def available() -> Optional[str]:
    """First usable opener on this machine, or None."""
    for name in CANDIDATES:
        if shutil.which(name):
            return name
    return None


def _mpv_android(target: str, is_file: bool) -> bool:
    args = [
        "am", "start", "--user", "0",
        "-a", "android.intent.action.VIEW",
        "-d", target,
        "-n", "is.xyz.mpv/.MPVActivity",
    ]
    if is_file:
        args += ["-t", "video/*"]
    try:
        return subprocess.run(args, capture_output=True, timeout=5).returncode == 0
    except Exception:
        # Anything here means we cannot drive mpv-android; caller falls back.
        return False


def open_target(
    target: str,
    *,
    title: str = "",
    cookie: str = "",
    ua: str = "",
    local: bool = False,
    prefer_external: bool = False,
) -> Optional[str]:
    """Open a URL or local path with whatever is installed.

    Returns the program used, or None when no opener exists.
    """
    if shutil.which("termux-open"):
        if not prefer_external:
            uri = f"file://{target}" if local else target
            if _mpv_android(uri, local):
                return "mpv-android"
        subprocess.Popen(["termux-open", "--view", target], start_new_session=True)
        return "termux-open"

    mpv = shutil.which("mpv") if not prefer_external else None
    if mpv:
        args = [mpv, target]
        if title:
            args.append(f"--force-media-title={title}")
        if cookie or ua:
            args.append(f"--http-header-fields=Cookie: {cookie}\nUser-Agent: {ua}")
        subprocess.Popen(args, start_new_session=True)
        return "mpv"

    opener = shutil.which("open") or shutil.which("xdg-open")
    if opener:
        subprocess.Popen([opener, target], start_new_session=True)
        return Path(opener).name
    return None
