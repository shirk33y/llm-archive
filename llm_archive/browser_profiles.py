from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from llm_archive.auth.browser_cookies import extract_browser_cookies, firefox_browser_dirs


@dataclass(frozen=True)
class BrowserProfile:
    browser: str
    family: str
    profile: str
    path: Path
    cookie_count: int | None = None


def detect_browser_profiles(domains: tuple[str, ...] = ()) -> list[BrowserProfile]:
    profiles = _firefox_profiles(domains)
    profiles.extend(_chromium_profiles())
    return profiles


def verified_cookie_profiles(domains: tuple[str, ...]) -> list[BrowserProfile]:
    profiles = detect_browser_profiles()
    verified = []
    for profile in profiles:
        try:
            cookies = extract_browser_cookies(
                profile.browser,
                browser_dir=str(profile.path),
                domains=domains,
            )
        except Exception:
            continue
        if cookies:
            verified.append(
                BrowserProfile(
                    profile.browser,
                    profile.family,
                    profile.profile,
                    profile.path,
                    len(cookies),
                )
            )
    return verified


def _firefox_profiles(domains: tuple[str, ...]) -> list[BrowserProfile]:
    results: list[BrowserProfile] = []
    for root in firefox_browser_dirs():
        root_path = Path(root).expanduser()
        if not root_path.exists():
            continue
        for cookie_db in root_path.glob("**/cookies.sqlite"):
            profile_dir = cookie_db.parent
            count = None
            if domains:
                try:
                    count = len(extract_browser_cookies("firefox", browser_dir=str(profile_dir), domains=domains))
                except Exception:
                    count = 0
            results.append(
                BrowserProfile(_browser_name(root_path), "firefox", profile_dir.name, profile_dir, count)
            )
    return _unique(results)


def _chromium_profiles() -> list[BrowserProfile]:
    results = []
    for browser, root in _chromium_roots():
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.is_dir() and (child / "Cookies").exists():
                results.append(BrowserProfile(browser, "chromium", child.name, child))
            if child.is_dir() and (child / "Network" / "Cookies").exists():
                results.append(BrowserProfile(browser, "chromium", child.name, child))
    return _unique(results)


def _chromium_roots() -> list[tuple[str, Path]]:
    home = Path.home()
    if sys.platform == "darwin":
        app = home / "Library" / "Application Support"
        return [
            ("chrome", app / "Google" / "Chrome"),
            ("chromium", app / "Chromium"),
            ("brave", app / "BraveSoftware" / "Brave-Browser"),
            ("edge", app / "Microsoft Edge"),
            ("opera", app / "com.operasoftware.Opera"),
        ]
    return [
        ("chrome", home / ".config" / "google-chrome"),
        ("chromium", home / ".config" / "chromium"),
        ("brave", home / ".config" / "BraveSoftware" / "Brave-Browser"),
        ("edge", home / ".config" / "microsoft-edge"),
        ("opera", home / ".config" / "opera"),
        ("chrome", home / ".var" / "app" / "com.google.Chrome" / "config" / "google-chrome"),
        ("chromium", home / ".var" / "app" / "org.chromium.Chromium" / "config" / "chromium"),
        ("brave", home / ".var" / "app" / "com.brave.Browser" / "config" / "BraveSoftware" / "Brave-Browser"),
    ]


def _browser_name(root: Path) -> str:
    text = str(root).lower()
    if "waterfox" in text:
        return "waterfox"
    if "librewolf" in text:
        return "librewolf"
    return "firefox"


def _unique(profiles: list[BrowserProfile]) -> list[BrowserProfile]:
    seen = set()
    unique = []
    for profile in profiles:
        key = profile.path
        if key in seen:
            continue
        seen.add(key)
        unique.append(profile)
    return unique
