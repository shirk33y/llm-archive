from __future__ import annotations
import json
from pathlib import Path

AUTH_DIR = Path.home() / ".llm-archive" / "auth"


def auth_path(source_id: str) -> Path:
    return AUTH_DIR / f"{source_id}.json"


async def login_headful(source_id: str, url: str) -> dict:
    """Open headful browser, wait for user to log in, save and return storageState."""
    from playwright.async_api import async_playwright

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    out = auth_path(source_id)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(url)

        print(f"\nLog in to {url} in the browser window.")
        print("Press Enter here when done...")
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, input)

        state = await ctx.storage_state(path=str(out))
        await browser.close()

    return state


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
