from __future__ import annotations
import glob
import os
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

    logger.info(f"Extracted {len(cookies)} cookies from browser")
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


def extract_cookies_from_firefox(
    profile: str | None = None,
    *,
    browser_dir: str | None = None,
    domains: tuple[str, ...] | None = None,
) -> dict[str, str]:
    return cookies_to_dict(
        extract_firefox_cookies(profile, browser_dir=browser_dir, domains=domains)
    )


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
