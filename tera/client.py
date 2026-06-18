import re
import requests
from urllib.parse import urlparse, parse_qs
from typing import Optional
from .config import Config, API_DOMAIN, HEADERS, TERABOX_DOMAINS


class TeraBoxError(Exception):
    pass


class TeraBoxClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.cookies.set("ndus", config.auth.ndus)
        self.session.cookies.set("PANWEB", config.auth.panweb)
        # Auto-refresh tokens on initialization to prevent expired jsToken errors
        if config.auth.is_valid:
            self.refresh_tokens()

    def refresh_tokens(self) -> bool:
        """Fetch the main page to auto-update jsToken and bdstoken."""
        try:
            resp = self.session.get(f"{API_DOMAIN}/main", timeout=10)
            resp.raise_for_status()
            html = resp.text
            
            # Extract jsToken
            import urllib.parse
            js_token_match = re.search(r'["\']jsToken["\']\s*[:=]\s*["\']([^"\']+)["\']', html)
            if js_token_match:
                raw_val = js_token_match.group(1)
                decoded = urllib.parse.unquote(raw_val)
                token_match = re.search(r'fn\s*\(\s*["\']([a-fA-F0-9]+)["\']', decoded)
                if token_match:
                    self.config.auth.js_token = token_match.group(1)
                else:
                    self.config.auth.js_token = raw_val
            
            # Extract bdstoken
            bdstoken_patterns = [
                r'["\']bdstoken["\']\s*[:=]\s*["\']([a-fA-F0-9]{32})["\']',
                r'bdstoken\s*[:=]\s*["\']([a-fA-F0-9]{32})["\']',
                r'window\.bdstoken\s*[:=]\s*["\']([a-fA-F0-9]{32})["\']',
            ]
            for pat in bdstoken_patterns:
                m = re.search(pat, html)
                if m:
                    self.config.auth.bdstoken = m.group(1)
                    break
            
            self.config.save()
            return True
        except Exception:
            return False

    def _params(self, extra: Optional[dict] = None) -> dict:
        p = self.config.base_params()
        if extra:
            p.update(extra)
        return p

    def _check_errno(self, data: dict):
        errno = data.get("errno", -1)
        if errno != 0:
            raise TeraBoxError(f"API error (errno={errno}): {data.get('errmsg', 'unknown')}")

    # ── Share link resolution ──────────────────────────────────────────

    @staticmethod
    def parse_share_url(url: str) -> Optional[str]:
        """Extract shorturl/share id from a terabox share link."""
        import urllib.parse as urlparse
        try:
            parsed = urlparse.urlparse(url)
            qs = urlparse.parse_qs(parsed.query)
            if "surl" in qs:
                return qs["surl"][0]
        except Exception:
            pass

        patterns = [
            r"terabox\.(?:com|app|link)/s/([A-Za-z0-9_-]+)",
            r"1024terabox\.com/s/([A-Za-z0-9_-]+)",
            r"teraboxshare\.com/s/([A-Za-z0-9_-]+)",
            r"terasharefile\.com/s/([A-Za-z0-9_-]+)",
            r"terafileshare\.com/s/([A-Za-z0-9_-]+)",
            r"terasharelink\.com/s/([A-Za-z0-9_-]+)",
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                shorturl = m.group(1)
                # Strip leading '1' if it's a browser-redirected 23-character short URL
                if len(shorturl) == 23 and shorturl.startswith("1"):
                    return shorturl[1:]
                return shorturl
        return None

    def resolve_share_page(self, shorturl: str, pwd: str = "", dir_path: str = "") -> dict:
        """Fetch share page to get jsToken and file list."""
        params = {
            "shorturl": shorturl,
            "root": "1" if not dir_path else "0",
        }
        if dir_path:
            params["dir"] = dir_path
        if pwd:
            params["pwd"] = pwd

        resp = self.session.get(
            f"{API_DOMAIN}/share/list",
            params=self._params(params),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._check_errno(data)
        return data

    def get_share_files(self, url: str, pwd: str = "", dir_path: str = "") -> list:
        """Get file list from a share URL."""
        shorturl = self.parse_share_url(url)
        if not shorturl:
            raise TeraBoxError(f"Invalid share URL: {url}")

        data = self.resolve_share_page(shorturl, pwd, dir_path)
        return data.get("list", [])

    def get_share_dlink(self, url: str, fs_id: int, pwd: str = "") -> str:
        """Get direct download link for a shared file."""
        shorturl = self.parse_share_url(url)
        if not shorturl:
            raise TeraBoxError(f"Invalid share URL: {url}")

        params = {
            "shorturl": shorturl,
            "root": "1",
            "fid_list": f"[{fs_id}]",
            "channel": "dubox",
        }
        if pwd:
            params["pwd"] = pwd

        resp = self.session.get(
            f"{API_DOMAIN}/share/download",
            params=self._params(params),
            timeout=15,
        )
        resp.raise_for_status()

        # The download endpoint returns a 302 redirect
        if resp.status_code == 302:
            return resp.headers.get("Location", "")

        data = resp.json()
        if "dlink" in data:
            return data["dlink"]

        raise TeraBoxError("Could not get download link")

    # ── Drive operations ───────────────────────────────────────────────

    def list_files(self, path: str = "/", num: int = 100, page: int = 1) -> list:
        """List files in a directory."""
        params = {
            "dir": path,
            "num": str(num),
            "page": str(page),
            "order": "time",
            "desc": "1",
        }
        resp = self.session.get(
            f"{API_DOMAIN}/api/list",
            params=self._params(params),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._check_errno(data)
        return data.get("list", [])

    def get_file_info(self, path: str) -> dict:
        """Get info for a single file."""
        files = self.list_files(path, num=1)
        for f in files:
            if f.get("path") == path:
                return f
        raise TeraBoxError(f"File not found: {path}")

    def get_download_link(self, path: str) -> str:
        """Get direct download link for a file in your drive."""
        resp = self.session.get(
            f"{API_DOMAIN}/rest/2.0/pcs/file",
            params=self._params({"method": "download", "path": path}),
            timeout=15,
            allow_redirects=False,
        )
        if resp.status_code == 302:
            return resp.headers.get("Location", "")

        # Some responses include dlink in JSON
        try:
            data = resp.json()
            if "dlink" in data:
                return data["dlink"]
        except Exception:
            pass

        raise TeraBoxError(f"Could not get download link for: {path}")

    def create_directory(self, path: str) -> dict:
        """Create a directory."""
        data = {
            "path": path,
            "isdir": "1",
            "rtype": "3",
        }
        resp = self.session.post(
            f"{API_DOMAIN}/api/create",
            params=self._params(),
            data=data,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        self._check_errno(result)
        return result

    def delete(self, filelist: list[str]) -> dict:
        """Delete files/directories."""
        import json
        data = {
            "filelist": json.dumps(filelist),
            "type": "1",
        }
        resp = self.session.post(
            f"{API_DOMAIN}/api/filemanager",
            params=self._params({"opera": "delete"}),
            data=data,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        self._check_errno(result)
        return result

    def rename(self, path: str, new_name: str) -> dict:
        """Rename a file/directory."""
        import json
        data = {
            "filelist": json.dumps([{"path": path, "newname": new_name}]),
        }
        resp = self.session.post(
            f"{API_DOMAIN}/api/filemanager",
            params=self._params({"opera": "rename"}),
            data=data,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        self._check_errno(result)
        return result

    def create_share(self, filelist: list[int], period: int = 0) -> dict:
        """Create a share link for files."""
        import json
        data = {
            "fid_list": json.dumps(filelist),
            "schannel": "0",
            "channel_list": "[]",
            "period": str(period),
            "public": "1",
        }
        resp = self.session.post(
            f"{API_DOMAIN}/share/pset",
            params=self._params(),
            data=data,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        self._check_errno(result)
        return result

    def get_quota(self) -> dict:
        """Get account quota/storage info."""
        resp = self.session.get(
            f"{API_DOMAIN}/api/quota",
            params=self._params(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._check_errno(data)
        return data
