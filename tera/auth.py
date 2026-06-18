import re
import urllib.parse
import requests
from .config import Config, AuthConfig, API_DOMAIN, HEADERS


class AuthError(Exception):
    pass


def extract_tokens(ndus: str) -> tuple[str, str]:
    """Fetch main page using ndus and extract jsToken and bdstoken."""
    session = requests.Session()
    session.cookies.set("ndus", ndus)
    session.cookies.set("PANWEB", "1")
    session.headers.update(HEADERS)

    try:
        resp = session.get(f"{API_DOMAIN}/main", timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        raise AuthError(f"Failed to fetch main page: {e}")

    js_token = ""
    js_token_match = re.search(r'["\']jsToken["\']\s*[:=]\s*["\']([^"\']+)["\']', html)
    if js_token_match:
        raw_val = js_token_match.group(1)
        decoded = urllib.parse.unquote(raw_val)
        token_match = re.search(r'fn\s*\(\s*["\']([a-fA-F0-9]+)["\']', decoded)
        if token_match:
            js_token = token_match.group(1)
        else:
            js_token = raw_val

    if not js_token:
        raise AuthError("Could not extract jsToken from main page. Please make sure your ndus cookie is valid and you are logged in.")

    bdstoken = ""
    bdstoken_patterns = [
        r'["\']bdstoken["\']\s*[:=]\s*["\']([a-fA-F0-9]{32})["\']',
        r'bdstoken\s*[:=]\s*["\']([a-fA-F0-9]{32})["\']',
        r'window\.bdstoken\s*[:=]\s*["\']([a-fA-F0-9]{32})["\']',
    ]
    for pat in bdstoken_patterns:
        m = re.search(pat, html)
        if m:
            bdstoken = m.group(1)
            break

    return js_token, bdstoken


def login_interactive(config: Config) -> Config:
    """Interactive login - prompts user for ndus cookie and auto-extracts other tokens."""
    print("╔══════════════════════════════════════════════════╗")
    print("║        TeraBox CLI - Authentication Setup        ║")
    print("╠══════════════════════════════════════════════════╣")
    print("║  1. Open https://1024terabox.com and login       ║")
    print("║  2. Press F12 → Application/Storage → Cookies    ║")
    print("║  3. Find and copy the 'ndus' cookie value        ║")
    print("║     (Typically starts with '2:')                 ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    ndus = input("ndus cookie value: ").strip()
    if not ndus:
        raise AuthError("ndus cookie is required")

    print("\n[dim]Auto-extracting jsToken and bdstoken from TeraBox...[/dim]")
    try:
        js_token, bdstoken = extract_tokens(ndus)
        print("[green]Successfully extracted tokens![/green]")
    except Exception as e:
        raise AuthError(f"Auto-extraction failed: {e}")

    config.auth = AuthConfig(ndus=ndus, js_token=js_token, bdstoken=bdstoken)
    config.save()

    return config


def verify_auth(config: Config) -> dict:
    """Verify authentication by fetching user info. Returns user info dict."""
    if not config.auth.is_valid:
        raise AuthError("Not authenticated. Run: tera auth login")

    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set("ndus", config.auth.ndus)
    session.cookies.set("PANWEB", config.auth.panweb)

    params = config.base_params()

    # 1. Fetch user profile info
    try:
        resp = session.get(f"{API_DOMAIN}/passport/get_info", params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise AuthError(f"Network error: {e}")

    data = resp.json()
    if data.get("code") != 0:
        raise AuthError(f"Auth failed (code={data.get('code')}): {data.get('msg', 'unknown error')}")

    user_data = data.get("data", {})
    uname = user_data.get("display_name", "unknown")

    # 2. Fetch storage quota
    try:
        resp_quota = session.get(f"{API_DOMAIN}/api/quota", params=params, timeout=10)
        resp_quota.raise_for_status()
    except requests.RequestException as e:
        raise AuthError(f"Network error: {e}")

    quota_data = resp_quota.json()
    used = quota_data.get("used", 0)
    total = quota_data.get("total", 0)

    return {
        "uname": uname,
        "used": used,
        "total": total
    }
