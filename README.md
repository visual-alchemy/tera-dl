# TeraBox CLI 🚀

A Python-based command-line interface (CLI) tool designed to bypass standard download/upload limitations and manage your TeraBox cloud storage directly from your terminal.

---

## Features

- **Auth Verification**: Interactive setup for cookies/tokens.
- **Drive Navigation**: List, create directories, and delete files on your TeraBox drive.
- **Limit Bypass Downloading**: Download files from your drive or public TeraBox sharing links with multi-worker support.
- **Rapid Upload**: Computes chunked MD5 hashes to support TeraBox's rapid upload mechanism (instantly adds files if they already exist in their cloud database).
- **Beautiful Output**: Leverages the `rich` library for rendering clean terminal tables and active progress bars.

---

## Installation

### Option 1: pipx (Recommended)
`pipx` installs Python CLI tools globally without needing a virtual environment. It automatically creates an isolated environment for each package.

```bash
# Install pipx if you don't have it
python3 -m pip install --user pipx
python3 -m pipx ensurepath

# Install tera-dl
pipx install tera-dl
```

After installation, `tera` and `tera-dl` commands are available globally.

### Option 2: pip (user install)
```bash
pip install --user tera-dl
```

### Option 3: From source
```bash
git clone https://github.com/visual-alchemy/tera-dl.git
cd tera-dl
pip install .
```

---

## Authentication Setup

Because TeraBox uses session protection, you need to import your authenticated browser session into the CLI.

1. Open your web browser and log in to [1024TeraBox](https://1024terabox.com).
2. Open Developer Tools (**F12** or **Cmd + Option + I** on macOS) → **Application** → **Cookies** → `https://1024terabox.com`.
3. Find and copy these cookie values:
   - **`ndus`**: Your session cookie (typically starts with `2:`).
   - **`BDUSS`**: Required for uploads. If not visible, uploads may still work via rapid upload.
4. Run the interactive login command:
   ```bash
   tera auth login
   ```
5. Paste the `ndus` and `BDUSS` values when prompted. `jsToken` and `bdstoken` are auto-extracted.

---

## TUI Mode (Midnight Commander Style)

Launch an interactive dual-pane file manager — local filesystem on the left, TeraBox drive on the right:

```bash
tera tui
```

| Key | Action |
|---|---|
| `Tab` | Switch between panes |
| `Enter` / `→` | Open folder |
| `←` / `Backspace` | Go to parent directory |
| `Space` / `Insert` | Mark / unmark file (sets range anchor) |
| `Shift` + `↑`/`↓` | Extend mark range from the anchor |
| `Shift` + Click | Mark range to the clicked row |
| `a` | Mark all / clear all (toggle) |
| `s` | Sort: name/size/time, asc/desc |
| `[` / `]` | Narrow / widen the local pane |
| `F2` | Rename |
| `F5` | Copy (upload/download marked or selected files, folders recursive) |
| `F7` | Create directory |
| `F8` | Delete (marked or selected) |
| `v` | View file (download to temp, open in player) |
| `r` | Refresh pane |
| `t` | Toggle dark/light theme |
| `?` | Keybinding help |
| `q` | Quit |

---

## CLI Command Reference

### 🔐 Authentication

#### `tera auth login`
Launches the interactive setup wizard to save session cookies.
```bash
tera auth login
```

#### `tera auth status`
Checks your current session credentials and displays username and storage usage.
```bash
tera auth status
```

---

### 📂 File Management

#### `tera ls [PATH]`
Lists files and directories at the specified path (default: `/`).
* **Options:**
  - `-n, --num INTEGER`: Maximum items to list (default: 100).
```bash
tera ls /Documents
```

#### `tera mkdir [PATH]`
Creates a new directory in your drive.
```bash
tera mkdir /Documents/Backups
```

#### `tera rm [PATH]`
Deletes a file or directory.
* **Options:**
  - `-y, --yes`: Skip delete confirmation prompt.
```bash
tera rm /Documents/old_file.txt -y
```

---

### 📥 Downloads

#### `tera dl [SOURCES]...`
Downloads files from drive paths or public share links (supports multiple links/paths separated by space).
* **Arguments:**
  - `SOURCES`: Can be paths inside your drive (e.g. `/Videos/movie.mp4`) or public sharing links (e.g. `https://terabox.com/s/1abcde...`).
* **Options:**
  - `-o, --output PATH`: Custom output directory (default: `~/Downloads/tera`).
  - `-w, --workers INTEGER`: Number of parallel workers to use.
  - `-p, --pwd TEXT`: Password for password-protected share links.
  - `--retries INTEGER`: Retry passes for sources that had failures (default: `1`). Each pass re-resolves only the failed sources; files already complete on disk are skipped by size, so nothing is re-downloaded.

```bash
# Download from drive
tera dl /Videos/movie.mp4

# Download multiple public sharing links at once
tera dl https://terabox.com/s/link1 https://terabox.com/s/link2 -o ./my-downloads

# Try failed links up to 3 extra times before giving up
tera dl https://terabox.com/s/link1 --retries 3
```

Exits with status `1` if files are still failing after the retry passes.

---

### 📤 Uploads

#### `tera ul [LOCAL_PATH]`
Uploads a local file to your TeraBox drive.
* **Options:**
  - `-r, --remote TEXT`: Specify remote destination path (default: `/[filename]`).
```bash
tera ul ./backup.zip -r /Documents/backup.zip
```

#### `tera sync LOCAL_DIR REMOTE_DIR`
Uploads files from a local directory into matching remote subfolders by name,
then moves the uploaded files into `LOCAL_DIR/uploaded`. Dry-run by default.
* **Options:**
  - `--apply`: Actually upload and move (default: preview only).
  - `--uploaded-dir PATH`: Move uploaded files here (default: `<LOCAL_DIR>/uploaded`).
  - `--limit INTEGER`: Upload at most N files this run.
```bash
# Preview the plan
tera sync ~/storage/shared/DCIM/Camera /Photos

# Upload at most 20 files this run
tera sync ~/storage/shared/DCIM/Camera /Photos --apply --limit 20
```

---

### ⚙️ Utility & Configs

#### `tera info`
Displays current storage details (used vs. total space).
```bash
tera info
```

#### `tera view [PATH]`
Plays a drive file without a full manual download. On Android it downloads to a
temp folder and opens it in mpv-android; on desktop it streams in mpv.
```bash
tera view /Videos/movie.mp4
```

#### `tera share [PATH]`
Creates a public share link for a file on your drive.
* **Options:**
  - `--period INTEGER`: Expiry period in days (default: `0` for permanent links).
```bash
tera share /Documents/presentation.pdf
```

#### `tera config`
Views or modifies the configuration settings.
* **Options:**
  - `--set-download-dir PATH`: Change default download directory.
  - `--set-workers INTEGER`: Change default parallel workers count.
```bash
# Set download directory
tera config --set-download-dir /Users/user/Downloads

# View current configuration
tera config
```
