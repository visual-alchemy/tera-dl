import re
import time
from datetime import datetime
import requests
from typing import Optional
from .config import Config, API_DOMAIN, HEADERS, TERABOX_DOMAINS
from .auth import scrape_tokens

# Refresh jsToken/bdstoken when older than this, or sooner after a failure.
TOKEN_TTL_SECONDS = 300
REFRESH_COOLDOWN_SECONDS = 60


class TeraBoxError(Exception):
    pass


class TeraBoxClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.cookies.set("ndus", config.auth.ndus)
        self.session.cookies.set("PANWEB", config.auth.panweb)
        if config.auth.bduss:
            self.session.cookies.set("BDUSS", config.auth.bduss)
        self._last_refresh_attempt = 0.0

    def _tokens_fresh(self) -> bool:
        """True if the stored tokens were scraped less than TOKEN_TTL_SECONDS ago."""
        ts = self.config.auth.tokens_refreshed_at
        if not ts:
            return False
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return False
        return (datetime.now() - dt).total_seconds() < TOKEN_TTL_SECONDS

    def _ensure_tokens(self) -> None:
        """Refresh tokens lazily, at most once per REFRESH_COOLDOWN_SECONDS."""
        now = time.monotonic()
        if now - self._last_refresh_attempt < REFRESH_COOLDOWN_SECONDS:
            return
        if not self._tokens_fresh():
            self._last_refresh_attempt = now
            self.refresh_tokens()

    def refresh_tokens(self) -> bool:
        """Fetch the main page to auto-update jsToken and bdstoken."""
        try:
            js_token, bdstoken, bduss = scrape_tokens(self.session)
            self.config.auth.js_token = js_token
            self.config.auth.bdstoken = bdstoken
            if bduss:
                self.config.auth.bduss = bduss
            self.config.auth.tokens_refreshed_at = datetime.now().isoformat()
            self.config.save()
            return True
        except Exception:
            return False

    def _params(self, extra: Optional[dict] = None) -> dict:
        p = self.config.base_params()
        if extra:
            p.update(extra)
        return p

    def _check_errno(self, data: dict, context: str = ""):
        errno = data.get("errno", -1)
        if errno != 0:
            msg = f"API error (errno={errno}): {data.get('errmsg', 'unknown')}"
            if context:
                msg = f"[{context}] {msg}"
            # Log all non-zero API errors for debugging
            from .config import CONFIG_DIR
            try:
                log = CONFIG_DIR / "api_errors.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                from datetime import datetime
                with open(log, "a") as f:
                    f.write(f"{datetime.now().isoformat()} {msg}\n")
                    f.write(f"  full response: {data}\n")
            except Exception:
                pass
            if errno == 400141:
                raise TeraBoxError(f"rate_limit")
            raise TeraBoxError(msg)

    @staticmethod
    def _looks_like_auth_error(errmsg) -> bool:
        m = str(errmsg).lower()
        return any(
            kw in m
            for kw in ("token", "login", "auth", "expire", "expired", "bdstoken", "jstoken", "user not")
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        allow_redirects: bool = True,
        timeout: int = 15,
        rate_limit_delays=(15, 30, 60, 120, 300),
        connection_retries: int = 5,
    ) -> dict:
        """Single request with backoff on rate-limit (400141) / connection errors and
        one auth-refresh retry. Returns parsed JSON, or {'_redirect': url} on a 302."""
        rate_attempts = 0
        conn_attempts = 0
        refreshed = False
        while True:
            try:
                resp = self.session.request(
                    method, url, params=params, data=data, files=files,
                    allow_redirects=allow_redirects, timeout=timeout,
                )
                if resp.status_code == 302:
                    return {"_redirect": resp.headers.get("Location", "")}
                resp.raise_for_status()
                data = resp.json()
            except requests.exceptions.ConnectionError as e:
                if conn_attempts < connection_retries:
                    conn_attempts += 1
                    time.sleep(min(2 ** (conn_attempts - 1), 30))
                    continue
                raise TeraBoxError(f"Connection failed after {connection_retries + 1} attempts: {e}")
            except requests.exceptions.RequestException as e:
                raise TeraBoxError(f"HTTP request failed: {e}")

            errno = data.get("errno", 0)
            if errno == 0:
                return data
            if errno == 400141:
                if rate_attempts < len(rate_limit_delays):
                    wait = rate_limit_delays[rate_attempts]
                    rate_attempts += 1
                    time.sleep(wait)
                    continue
                raise TeraBoxError("rate_limit")
            if not refreshed and self._looks_like_auth_error(data.get("errmsg", "")):
                refreshed = True
                if self.refresh_tokens():
                    continue
            self._check_errno(data)

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

        _shorturl_re = re.compile(
            r"(?:https?://)?(?:www\.)?(" + "|".join(
                re.escape(d) for d in TERABOX_DOMAINS
            ) + r")/s/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        )
        m = _shorturl_re.search(url)
        if m:
            shorturl = m.group(2)
            # Strip leading '1' if it's a browser-redirected 23-character short URL
            if len(shorturl) == 23 and shorturl.startswith("1"):
                return shorturl[1:]
            return shorturl
        return None

    def resolve_share_page(self, shorturl: str, pwd: str = "", dir_path: str = "") -> dict:
        """Fetch share page to get jsToken and file list."""
        self._ensure_tokens()
        params = {
            "shorturl": shorturl,
            "root": "1" if not dir_path else "0",
        }
        if dir_path:
            params["dir"] = dir_path
        if pwd:
            params["pwd"] = pwd

        return self._request_json(
            "GET", f"{API_DOMAIN}/share/list", params=self._params(params), timeout=15,
        )

    def get_share_files(self, url: str, pwd: str = "", dir_path: str = "") -> list:
        """Get file list from a share URL."""
        shorturl = self.parse_share_url(url)
        if not shorturl:
            raise TeraBoxError(f"Invalid share URL: {url}")

        data = self.resolve_share_page(shorturl, pwd, dir_path)
        return data.get("list", [])

    def get_share_dlink(self, url: str, fs_id: int, pwd: str = "") -> str:
        """Get direct download link for a shared file."""
        self._ensure_tokens()
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

        data = self._request_json(
            "GET", f"{API_DOMAIN}/share/download", params=self._params(params), timeout=15,
        )

        # The download endpoint returns a 302 redirect
        if "_redirect" in data:
            return data["_redirect"]

        if "dlink" in data:
            return data["dlink"]

        raise TeraBoxError("Could not get download link")

    # ── Drive operations ───────────────────────────────────────────────

    def list_files(self, path: str = "/", num: int = 100, page: int = 1) -> list:
        """List files in a directory."""
        self._ensure_tokens()
        params = {
            "dir": path,
            "num": str(num),
            "page": str(page),
            "order": "time",
            "desc": "1",
        }
        data = self._request_json(
            "GET", f"{API_DOMAIN}/api/list", params=self._params(params), timeout=15,
        )
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
        self._ensure_tokens()
        data = self._request_json(
            "GET", f"{API_DOMAIN}/rest/2.0/pcs/file",
            params=self._params({"method": "download", "path": path}),
            allow_redirects=False, timeout=15,
        )
        if "_redirect" in data:
            return data["_redirect"]

        # Some responses include dlink in JSON
        if "dlink" in data:
            return data["dlink"]

        raise TeraBoxError(f"Could not get download link for: {path}")

    def create_directory(self, path: str) -> dict:
        """Create a directory."""
        self._ensure_tokens()
        data = {
            "path": path,
            "isdir": "1",
            "rtype": "3",
        }
        return self._request_json(
            "POST", f"{API_DOMAIN}/api/create", params=self._params(), data=data, timeout=15,
        )

    def delete(self, filelist: list[str]) -> dict:
        """Delete files/directories (moves to recycle bin, handles non-empty dirs)."""
        self._ensure_tokens()
        import json
        data = {
            "filelist": json.dumps(filelist),
            "type": "0",
        }
        return self._request_json(
            "POST", f"{API_DOMAIN}/api/filemanager",
            params=self._params({"opera": "delete", "bdstoken": self.config.auth.bdstoken}),
            data=data, timeout=15,
        )

    def rename(self, path: str, new_name: str) -> dict:
        """Rename a file/directory."""
        self._ensure_tokens()
        import json
        data = {
            "filelist": json.dumps([{"path": path, "newname": new_name}]),
        }
        return self._request_json(
            "POST", f"{API_DOMAIN}/api/filemanager",
            params=self._params({"opera": "rename", "bdstoken": self.config.auth.bdstoken}),
            data=data, timeout=15,
        )

    def create_share(self, filelist: list[int], period: int = 0) -> dict:
        """Create a share link for files."""
        self._ensure_tokens()
        import json
        data = {
            "fid_list": json.dumps(filelist),
            "schannel": "0",
            "channel_list": "[]",
            "period": str(period),
            "public": "1",
        }
        return self._request_json(
            "POST", f"{API_DOMAIN}/share/pset", params=self._params(), data=data, timeout=15,
        )

    def get_quota(self) -> dict:
        """Get account quota/storage info."""
        self._ensure_tokens()
        return self._request_json(
            "GET", f"{API_DOMAIN}/api/quota", params=self._params(), timeout=10,
        )
