from __future__ import annotations
import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Callable, Awaitable

from llm_archive.logging import get_logger

logger = get_logger("auth")

AUTH_DIR = Path.home() / ".llm-archive" / "auth"


def auth_path(source_id: str) -> Path:
    return AUTH_DIR / f"{source_id}.json"


async def login_headful(source_id: str, url: str) -> dict:
    """Connect to user's real Chrome via remote debugging, save storageState."""
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    out = auth_path(source_id)

    async def detect_login(ctx, page):
        """Detect Claude login by checking for session cookies."""
        for _ in range(300):
            cookies = await ctx.cookies()
            names = {c["name"] for c in cookies}
            if names & {"sessionKey", "__Secure-next-auth.session-token", "activitySessionId"}:
                return True
            await asyncio.sleep(1)
        return False

    return await login_with_detection(source_id, url, detect_login, out)


async def login_with_detection(
    source_id: str,
    url: str,
    detect_login: Callable[[any, any], Awaitable[bool]],
    output_path: Path,
    timeout: int = 300
) -> dict:
    """Generic login function that uses a callback to detect login completion."""
    from playwright.async_api import async_playwright

    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(f"{source_id.capitalize()} login required. Please press ENTER to open browser and login.")
    input()

    chrome = _find_chrome()
    chrome_args = chrome if isinstance(chrome, list) else [chrome]
    chrome_profile = _find_chrome_profile()
    proc = subprocess.Popen([
        *chrome_args,
        "--remote-debugging-port=9222",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--ozone-platform=x11",
        "--log-level=3",
        f"--user-data-dir={chrome_profile}",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    time.sleep(2)

    try:
        async with async_playwright() as p:
            logger.info(f"connecting to Chrome DevTools")
            logger.info("Waiting for login...")
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            ctx = browser.contexts[0]
            pages = ctx.pages
            page = pages[0] if pages else await ctx.new_page()

            await page.goto(url, wait_until="domcontentloaded")

            login_detected = await asyncio.wait_for(detect_login(ctx, page), timeout=timeout)
            if not login_detected:
                raise TimeoutError("Timed out waiting for login")

            logger.info(f"login detected — saving session")
            state = await ctx.storage_state(path=str(output_path))
            await browser.close()
            logger.info("closing browser")
    finally:
        proc.terminate()
        proc.wait()

    return state


def _find_chrome_profile() -> Path:
    """Return path to existing Chrome/Chromium user profile directory."""
    candidates = [
        Path.home() / ".var/app/com.google.Chrome/config/google-chrome",
        Path.home() / ".var/app/org.chromium.Chromium/config/chromium",
        Path.home() / ".config/google-chrome",
        Path.home() / ".config/chromium",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Fallback: fresh profile in our auth dir
    return AUTH_DIR / "chrome-profile"


def _find_chrome() -> str | list:
    import shutil
    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]
    for c in candidates:
        if shutil.which(c):
            return c

    # Flatpak wrappers — return as list so Popen handles args correctly
    if shutil.which("flatpak"):
        for app_id in ("com.google.Chrome", "org.chromium.Chromium"):
            result = subprocess.run(
                ["flatpak", "info", app_id], capture_output=True
            )
            if result.returncode == 0:
                return ["flatpak", "run", app_id]

    raise RuntimeError(
        "Could not find Chrome or Chromium. Install one and try again.\n"
        "e.g. flatpak install flathub org.chromium.Chromium"
    )


async def load_cookies(source_id: str) -> dict[str, str]:
    """Load cookies from saved storageState as a dict for use in httpx headers."""
    path = auth_path(source_id)
    if not path.exists():
        raise FileNotFoundError(
            f"No auth found for '{source_id}'. Run `llm-archive init {source_id}` first."
        )
    state = json.loads(path.read_text())
    cookies = {c["name"]: c["value"] for c in state.get("cookies", [])}
    return cookies


async def extract_cookies_headless(source_id: str, url: str) -> dict[str, str]:
    """Load existing storageState headlessly and return cookies."""
    from playwright.async_api import async_playwright

    path = auth_path(source_id)
    if not path.exists():
        raise FileNotFoundError(
            f"No auth found for '{source_id}'. Run `llm-archive init {source_id}` first."
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=str(path))
        page = await ctx.new_page()
        await page.goto(url)
        state = await ctx.storage_state()
        await browser.close()

    return {c["name"]: c["value"] for c in state.get("cookies", [])}
