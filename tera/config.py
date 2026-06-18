import json
import os
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

CONFIG_DIR = Path.home() / ".config" / "tera"
CONFIG_FILE = CONFIG_DIR / "config.json"

TERABOX_DOMAINS = [
    "terabox.com",
    "terabox.app",
    "1024terabox.com",
    "teraboxshare.com",
    "teraboxlink.com",
    "terasharefile.com",
    "terafileshare.com",
    "terasharelink.com",
]

API_DOMAIN = "https://1024terabox.com"
APP_ID = "250528"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Referer": "https://1024terabox.com/main?category=all",
    "Origin": "https://1024terabox.com",
}


@dataclass
class AuthConfig:
    ndus: str = ""
    js_token: str = ""
    bdstoken: str = ""
    panweb: str = "1"

    @property
    def is_valid(self) -> bool:
        return bool(self.ndus and self.js_token)

    def cookie_string(self) -> str:
        parts = [f"ndus={self.ndus}", f"PANWEB={self.panweb}"]
        return "; ".join(parts)


@dataclass
class Config:
    auth: AuthConfig = field(default_factory=AuthConfig)
    download_dir: str = str(Path.home() / "Downloads" / "tera")
    workers: int = 4
    chunk_size: int = 8192

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                auth = AuthConfig(**data.pop("auth", {}))
                return cls(auth=auth, **data)
            except (json.JSONDecodeError, TypeError):
                pass
        return cls()

    def base_params(self) -> dict:
        return {
            "app_id": APP_ID,
            "web": "1",
            "channel": "dubox",
            "clienttype": "0",
            "jsToken": self.auth.js_token,
        }
