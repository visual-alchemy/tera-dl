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

### 1. Set Up Virtual Environment (Recommended)
Before installing, it's recommended to isolate your dependencies inside a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Project Dependencies
Install the package in **editable mode** so that changes to the code are instantly reflected in the CLI executable:

```bash
pip install -e .
```

After installation, the global command `tera` will be available in your activated environment.

---

## Authentication Setup

Because TeraBox uses session protection, you need to import your authenticated browser session into the CLI.

1. Open your web browser and log in to [TeraBox](https://www.terabox.com).
2. Open Developer Tools (**F12** or **Cmd + Option + I** on macOS) and navigate to the **Network** tab.
3. Refresh the page or click on any directory.
4. Run the interactive login command in your terminal:
   ```bash
   tera auth login
   ```
5. Extract and paste the following values when prompted:
   - **`ndus`**: Go to Developer Tools -> **Application** (or **Storage**) -> **Cookies** -> `https://www.terabox.com`, find `ndus`, and copy the entire value (starts with `2:`).
   - **`jsToken`**: Go to the **Network** tab search (Cmd/Ctrl + F) for `jsToken` and extract the value from any query parameter.
   - **`bdstoken`**: Go to the **Console** tab, run `locals.bdstoken`, and copy the 32-character output.

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

```bash
# Download from drive
tera dl /Videos/movie.mp4

# Download multiple public sharing links at once
tera dl https://terabox.com/s/link1 https://terabox.com/s/link2 -o ./my-downloads
```

---

### 📤 Uploads

#### `tera ul [LOCAL_PATH]`
Uploads a local file to your TeraBox drive.
* **Options:**
  - `-r, --remote TEXT`: Specify remote destination path (default: `/[filename]`).
```bash
tera ul ./backup.zip -r /Documents/backup.zip
```

---

### ℹ️ Utility & Configs

#### `tera info`
Displays current storage details (used vs. total space).
```bash
tera info
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
