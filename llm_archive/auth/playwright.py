from __future__ import annotations
import json
import subprocess
import time
from pathlib import Path

AUTH_DIR = Path.home() / ".llm-archive" / "auth"


def auth_path(source_id: str) -> Path:
    return AUTH_DIR / f"{source_id}.json"


async def login_headful(source_id: str, url: str) -> dict:
    """Connect to user's real Chrome via remote debugging, save storageState."""
    from playwright.async_api import async_playwright

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    out = auth_path(source_id)

    # Find Chrome/Chromium executable
    chrome = _find_chrome()

    print(f"\nLaunching your Chrome with remote debugging on port 9222...")
    print(f"Navigate to {url} and log in.")
    print("The session will be saved automatically once you're logged in.\n")

    # Use existing Chrome profile so Google login is already active
    chrome_profile = _find_chrome_profile()
    chrome_args = chrome if isinstance(chrome, list) else [chrome]
    proc = subprocess.Popen([
        *chrome_args,
        "--remote-debugging-port=9222",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",   # disable OS keyring so cookies are readable
        f"--user-data-dir={chrome_profile}",
        url,
    ])

    # Give Chrome a moment to start
    time.sleep(2)

    async with async_playwright() as p:
        # Connect to the running Chrome
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        pages = ctx.pages
        page = pages[0] if pages else await ctx.new_page()

        # Wait until session cookie appears (means user is logged in)
        print("Waiting for login (up to 5 minutes)...")
        import asyncio
        for _ in range(300):
            cookies = await ctx.cookies()
            # Claude sets 'sessionKey' or '__Secure-next-auth.session-token' on login
            names = {c["name"] for c in cookies}
            if names & {"sessionKey", "__Secure-next-auth.session-token", "activitySessionId"}:
                print("Login detected — saving session.")
                break
            await asyncio.sleep(1)
        else:
            raise TimeoutError("Timed out waiting for login")

        state = await ctx.storage_state(path=str(out))
        await browser.close()

    proc.terminate()
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
