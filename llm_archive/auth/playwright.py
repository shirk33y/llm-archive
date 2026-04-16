from __future__ import annotations
import asyncio
import json
import os
import sys
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
    timeout: int = 300,
) -> dict:
    """Generic login function that uses a callback to detect login completion."""
    from playwright.async_api import async_playwright

    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(
        f"{source_id.capitalize()} login required. Please press ENTER to open browser and login."
    )
    input()

    import subprocess
    import urllib.request

    # Check if Chrome is already running with CDP
    existing_port = None
    for port in [9333, 9444, 9555]:
        try:
            resp = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=1)
            if resp.status == 200:
                existing_port = port
                logger.info(f"Found existing Chrome with CDP on port {port}")
                break
        except Exception:
            continue

    proc = None
    if existing_port:
        cdp_port = existing_port
    else:
        # Start new Chrome with isolated temp profile
        import tempfile

        chrome_profile = Path(tempfile.mkdtemp(prefix="chatgpt-chrome-"))
        cdp_port = 9333

        chrome_cmd = _find_chrome()
        if isinstance(chrome_cmd, list):
            chrome_args = chrome_cmd + [
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={chrome_profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "https://chatgpt.com",
            ]
        else:
            chrome_args = [
                chrome_cmd,
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={chrome_profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "https://chatgpt.com",
            ]

        logger.info("Starting Chrome...")
        proc = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for CDP to be ready
        for _ in range(30):
            import time

            time.sleep(1)
            try:
                resp = urllib.request.urlopen(
                    f"http://localhost:{cdp_port}/json/version", timeout=1
                )
                if resp.status == 200:
                    logger.info("Chrome ready with CDP")
                    break
            except Exception:
                continue
        else:
            proc.terminate()
            raise RuntimeError("Chrome failed to start with CDP")

    try:
        async with async_playwright() as p:
            logger.info("Connecting to Chrome via CDP...")
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            ctx = browser.contexts[0]
            pages = ctx.pages
            page = pages[0] if pages else await ctx.new_page()

            if page.url == "about:blank":
                logger.info("Navigating to ChatGPT...")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            login_detected = await asyncio.wait_for(detect_login(ctx, page), timeout=timeout)
            if not login_detected:
                raise TimeoutError("Timed out waiting for login")

            logger.info("login detected — extracting cookies...")

            # Extract cookies directly from the page context
            cookies = await ctx.cookies()

            # Also get access token from ChatGPT's session endpoint
            token_data = await page.evaluate("""async () => {
                try {
                    const resp = await fetch('/api/auth/session', {credentials: 'include'});
                    const data = await resp.json();
                    return {ok: resp.ok, accessToken: data.accessToken || null, user: data.user || null};
                } catch(e) {
                    return {ok: false, error: e.message};
                }
            }""")
            logger.info(f"Session data: {token_data}")

            state = {
                "cookies": cookies,
                "origins": [],
                "access_token": token_data.get("accessToken")
                if token_data and token_data.get("ok")
                else None,
                "user": token_data.get("user") if token_data and token_data.get("ok") else None,
            }

            import json

            output_path.write_text(json.dumps(state, indent=2))
            logger.info(
                f"Saved {len(cookies)} cookies, access_token: {'yes' if state['access_token'] else 'NO'}"
            )

            await browser.close()
            logger.info("closing browser")
    finally:
        if proc:
            proc.terminate()
            proc.wait()

    return state


def _find_chrome_profile() -> Path:
    """Return path to existing Chrome/Chromium profile for login."""
    # Check for existing profiles (flatpak Chrome stores here)
    candidates = [
        Path.home() / ".var/app/com.google.Chrome/config/google-chrome",
        Path.home() / ".var/app/org.chromium.Chromium/config/chromium",
        Path.home() / ".config/google-chrome",
        Path.home() / ".config/chromium",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Fallback: fresh profile in auth dir
    profile = AUTH_DIR / "chrome-login-profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _find_chrome() -> str | list:
    """Find Chrome or Chromium binary."""
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

    # Flatpak Chrome
    import subprocess

    if shutil.which("flatpak"):
        for app_id in ("com.google.Chrome", "org.chromium.Chromium"):
            result = subprocess.run(["flatpak", "info", app_id], capture_output=True)
            if result.returncode == 0:
                return ["flatpak", "run", "--share=network", app_id]

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
