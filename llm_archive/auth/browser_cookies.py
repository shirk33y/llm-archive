from __future__ import annotations
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from http.cookiejar import Cookie, CookieJar
from urllib.request import Request

from llm_archive.logging import get_logger

logger = get_logger("browser_cookies")

FIREFOX_MAX_SUPPORTED_DB_SCHEMA = 17


def firefox_browser_dirs() -> list[str]:
    if sys.platform in ("cygwin", "win32"):
        return [
            os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles"),
            os.path.expandvars(
                r"%LOCALAPPDATA%\Packages\Mozilla.Firefox_n80bbvh6b1yt2\LocalCache\Roaming\Mozilla\Firefox\Profiles"
            ),
        ]
    if sys.platform == "darwin":
        return [os.path.expanduser("~/Library/Application Support/Firefox/Profiles")]
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return [
        os.path.expanduser("~/.var/app/net.waterfox.waterfox/.waterfox"),
        os.path.expanduser("~/.waterfox"),
        os.path.expanduser("~/snap/waterfox/common/.waterfox"),
        os.path.join(config_home, "mozilla/firefox"),
        os.path.expanduser("~/.mozilla/firefox"),
        os.path.expanduser("~/.var/app/org.mozilla.firefox/config/mozilla/firefox"),
        os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox"),
        os.path.expanduser("~/snap/firefox/common/.mozilla/firefox"),
    ]


def _firefox_cookie_dbs(roots: list[str]) -> list[str]:
    results = []
    for root in map(os.path.abspath, roots):
        for pattern in ("", "*/", "Profiles/*/"):
            results.extend(glob.iglob(os.path.join(root, pattern, "cookies.sqlite")))
    return results


def _newest(paths: list[str]) -> str | None:
    return max(paths, key=lambda p: os.lstat(p).st_mtime, default=None)


def find_firefox_profile_dir(browser_dir: str | None = None, profile: str | None = None) -> Path:
    search_roots = [os.path.expanduser(browser_dir)] if browser_dir else firefox_browser_dirs()
    if profile:
        if os.path.sep in profile or (os.path.altsep and os.path.altsep in profile):
            search_roots = [profile]
        else:
            search_roots = [os.path.join(root, profile) for root in search_roots]

    db_path = _newest(_firefox_cookie_dbs(search_roots))
    if not db_path:
        raise FileNotFoundError(
            f"could not find Firefox/Waterfox profile in {search_roots}"
        )
    return Path(db_path).parent


def extract_firefox_cookies(
    profile: str | None = None,
    *,
    browser_dir: str | None = None,
    domains: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    search_roots = [os.path.expanduser(browser_dir)] if browser_dir else firefox_browser_dirs()
    if profile:
        if os.path.sep in profile or (os.path.altsep and os.path.altsep in profile):
            search_roots = [profile]
        else:
            search_roots = [os.path.join(root, profile) for root in search_roots]

    cookie_dbs = _firefox_cookie_dbs(search_roots)
    db_path = _newest(cookie_dbs)
    if not db_path:
        raise FileNotFoundError(
            f"could not find Firefox/Waterfox cookies database in {search_roots}"
        )

    logger.debug(f"Extracting cookies from: {db_path}")

    with tempfile.TemporaryDirectory(prefix="llm-archive-") as tmpdir:
        copy_path = os.path.join(tmpdir, "cookies.sqlite")
        shutil.copy(db_path, copy_path)
        conn = sqlite3.connect(copy_path)
        cursor = conn.cursor()

        schema_version = cursor.execute("PRAGMA user_version;").fetchone()[0]
        if schema_version > FIREFOX_MAX_SUPPORTED_DB_SCHEMA:
            logger.warning(
                f"Unsupported cookies DB schema v{schema_version}, may not parse correctly"
            )

        cursor.execute(
            "SELECT host, name, value, path, expiry, isSecure FROM moz_cookies"
        )
        cookies: list[dict[str, Any]] = []
        now = time.time()
        for host, name, value, path, expiry, is_secure in cursor.fetchall():
            if schema_version >= 16 and expiry is not None:
                expiry /= 1000
            if expiry is not None and expiry < now:
                continue
            if domains and not _domain_matches(host, domains):
                continue
            cookie = Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=host, domain_specified=bool(host), domain_initial_dot=host.startswith("."),
                path=path, path_specified=bool(path), secure=is_secure, expires=expiry,
                discard=False, comment=None, comment_url=None, rest={},
            )
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires,
                    "secure": cookie.secure,
                }
            )

        conn.close()

    logger.debug(f"Extracted {len(cookies)} cookies from browser")
    return cookies


def extract_browser_cookies(
    browser: str | None = None,
    profile: str | None = None,
    *,
    browser_dir: str | None = None,
    domains: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    browser = (browser or "firefox").lower()
    profile = browser_dir or profile
    if browser in {"firefox", "waterfox", "librewolf"}:
        return extract_firefox_cookies(profile=profile, domains=domains)
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except Exception as exc:
        raise RuntimeError("yt-dlp cookie support unavailable") from exc
    jar = extract_cookies_from_browser(browser, profile=profile)
    cookies: list[dict[str, Any]] = []
    now = time.time()
    for cookie in jar:
        if cookie.expires is not None and cookie.expires < now:
            continue
        if domains and not _domain_matches(cookie.domain, domains):
            continue
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
            }
        )
    logger.debug(f"Extracted {len(cookies)} cookies from {browser}")
    return cookies


def extract_firefox_local_storage(
    origin: str,
    profile: str | None = None,
    *,
    browser_dir: str | None = None,
) -> dict[str, str]:
    profile_dir = find_firefox_profile_dir(browser_dir, profile)
    storage_path = profile_dir / "storage" / "default" / _firefox_origin_dir(origin) / "ls" / "data.sqlite"
    if not storage_path.exists():
        raise FileNotFoundError(f"could not find Firefox localStorage database for {origin}")

    with tempfile.TemporaryDirectory(prefix="llm-archive-") as tmpdir:
        copy_path = Path(tmpdir) / "data.sqlite"
        shutil.copy(storage_path, copy_path)
        conn = sqlite3.connect(copy_path)
        try:
            rows = conn.execute("SELECT key, value FROM data").fetchall()
        finally:
            conn.close()

    values: dict[str, str] = {}
    for key, value in rows:
        if isinstance(value, bytes):
            values[str(key)] = value.decode("utf-8", errors="ignore")
        else:
            values[str(key)] = str(value)
    return values


def extract_browser_local_storage_value(
    origin: str,
    key: str,
    browser: str | None = None,
    profile: str | None = None,
    *,
    browser_dir: str | None = None,
) -> str:
    browser = (browser or "firefox").lower()
    if browser in {"firefox", "waterfox", "librewolf"}:
        return extract_firefox_local_storage(origin, profile, browser_dir=browser_dir)[key]
    profile_dir = Path(browser_dir or profile or "").expanduser()
    if not profile_dir:
        raise FileNotFoundError("No browser profile configured")
    return _extract_chromium_local_storage_value(profile_dir, key)


def _extract_chromium_local_storage_value(profile_dir: Path, key: str) -> str:
    storage_dir = profile_dir / "Local Storage" / "leveldb"
    if not storage_dir.exists():
        raise FileNotFoundError(f"could not find Chromium localStorage in {profile_dir}")
    key_bytes = key.encode()
    for path in sorted(storage_dir.glob("*")):
        if path.suffix not in {".ldb", ".log"}:
            continue
        data = path.read_bytes()
        if key_bytes not in data:
            continue
        text = data.decode("utf-8", errors="ignore")
        value = _find_local_storage_value(text, key)
        if value:
            return value
    raise FileNotFoundError(f"No {key} found in Chromium localStorage")


def _find_local_storage_value(text: str, key: str) -> str | None:
    start = text.find(key)
    if start < 0:
        return None
    tail = text[start : start + 4096]
    match = re.search(r"\{[^\{\}]{0,300}\"value\"\s*:\s*\"([^\"]+)\"[^\{\}]{0,300}\}", tail)
    if match:
        return json.dumps({"value": match.group(1)})
    match = re.search(r"(eyJ[a-zA-Z0-9_.-]{20,})", tail)
    if match:
        return match.group(1)
    return None


def cookies_to_dict(cookies: list[dict[str, Any]]) -> dict[str, str]:
    return {str(cookie["name"]): str(cookie["value"]) for cookie in cookies}


def cookie_header_for_url(cookies: list[dict[str, Any]], url: str) -> str:
    jar = CookieJar()
    for item in cookies:
        domain = str(item.get("domain") or "")
        path = str(item.get("path") or "/")
        jar.set_cookie(
            Cookie(
                version=0,
                name=str(item["name"]),
                value=str(item["value"]),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=path,
                path_specified=bool(path),
                secure=bool(item.get("secure")),
                expires=item.get("expires"),
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    req = Request(url)
    jar.add_cookie_header(req)
    return req.get_header("Cookie") or ""


def _domain_matches(host: str, domains: tuple[str, ...]) -> bool:
    normalized = host.lstrip(".").lower()
    for domain in domains:
        wanted = domain.lstrip(".").lower()
        if normalized == wanted or normalized.endswith(f".{wanted}"):
            return True
    return False


def _firefox_origin_dir(origin: str) -> str:
    return origin.removesuffix("/").replace("://", "+++").replace(":", "+")
