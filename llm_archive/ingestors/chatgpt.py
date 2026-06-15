"""
ChatGPT web ingestor.

Authentication: Uses Chrome CDP to fetch fresh access_token from browser session.
Connects to existing Chrome instance via remote debugging port (9333 by default).
If no Chrome with CDP is found, automatically launches Chrome with proper flags.

Rate limiting: The /conversation/{id} endpoint triggers 429 after ~2 rapid requests.
Adaptive MessageRateLimiter:
- Initial delay: 5s
- On 429: double delay (max 60s), enter conservative mode
- Conservative mode: decrease 2s per 20 successes until safe_delay (10s)
- At safe_delay: continue decreasing 0.5s per 20 successes until initial_delay
- Recovery from 60s to 5s takes ~35 minutes

API: Uses Bearer token auth from /api/auth/session, not cookie-based auth
(which returns 403).
"""

from __future__ import annotations
import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import httpx

from llm_archive.auth.browser_cookies import (
    cookie_header_for_url,
    cookies_to_dict,
    extract_firefox_cookies,
)
from llm_archive.auth.playwright import AUTH_DIR, auth_path, login_with_detection
from llm_archive.config import VALID_AUTH_MODES, load_config
from llm_archive.ingestors.base import BaseIngestor
from llm_archive.logging import get_logger
from llm_archive.ratelimit import MessageRateLimiter
from llm_archive.schema import IngestedMessage, IngestedThread

logger = get_logger("chatgpt")

LOGIN_URL = "https://chatgpt.com"
API_BASE = "https://chatgpt.com/backend-api"
CDP_PORT = 9333


def _find_chrome_binary() -> list[str]:
    """Find Chrome/Chromium binary. Returns command as list for subprocess."""
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
            return [c]

    if shutil.which("flatpak"):
        for app_id in ("com.google.Chrome", "org.chromium.Chromium"):
            result = subprocess.run(["flatpak", "info", app_id], capture_output=True)
            if result.returncode == 0:
                return ["flatpak", "run", "--share=network", "--filesystem=/tmp", app_id]

    raise RuntimeError(
        "Could not find Chrome or Chromium. Install one and try again.\n"
        "e.g. flatpak install flathub org.chromium.Chromium"
    )


def _launch_chrome_with_cdp() -> tuple[subprocess.Popen, int]:
    """Launch Chrome with CDP debugging enabled. Returns (process, port)."""
    chrome_cmd = _find_chrome_binary()

    user_data_dir = Path(tempfile.mkdtemp(prefix="llm-archive-chrome-"))

    proc = subprocess.Popen(
        [
            *chrome_cmd,
            f"--remote-debugging-port={CDP_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--ozone-platform=x11",
            f"--user-data-dir={user_data_dir}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    time.sleep(2)

    return proc, CDP_PORT


def _cleanup_chrome_proc(proc: subprocess.Popen | None, user_data_dir: Path | None) -> None:
    """Clean up Chrome process and temp profile."""
    if proc:
        proc.terminate()
        proc.wait(timeout=5)
    if user_data_dir and user_data_dir.exists():
        shutil.rmtree(user_data_dir, ignore_errors=True)


BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "accept": "application/json",
    "content-type": "application/json",
}


class ChatGPTIngestor(BaseIngestor):
    source_id = "chatgpt"

    def __init__(
        self,
        auth_mode: str | None = None,
        browser_dir: str | None = None,
        browser_path: str | None = None,
        use_cdp: bool = False,
    ):
        config = load_config()
        chatgpt_config = config.ingestor(self.source_id)
        mode = auth_mode or chatgpt_config.mode or "cookies"
        if use_cdp:
            mode = "cdp"
        if mode not in VALID_AUTH_MODES:
            valid = ", ".join(sorted(VALID_AUTH_MODES))
            raise ValueError(f"Invalid ChatGPT auth mode: {mode!r}. Expected: {valid}")
        self._message_limiter = MessageRateLimiter()
        self._auth_mode = mode
        self._browser_dir = browser_dir or config.browser_dir
        self._browser_path = browser_path or config.browser_path
        self._use_cdp = mode == "cdp"

    async def requires_auth(self) -> bool:
        return True

    async def init(self, **kwargs) -> None:
        if self._auth_mode == "cdp":
            if not auth_path(self.source_id).exists() or kwargs.get("reauth"):
                await _login()
        else:
            if kwargs.get("reauth"):
                p = auth_path(self.source_id)
                if p.exists():
                    p.unlink()

    def _build_headers(self, token: str, cookies: dict[str, str]) -> dict[str, str]:
        """Build request headers with browser-like headers."""
        headers = dict(BROWSER_HEADERS)
        headers["authorization"] = f"Bearer {token}"
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if "oai-did" in cookies:
            headers["oai-device-id"] = cookies["oai-did"]
        return headers

    # Re-check threads if content_checked_at is older than this interval.
    MAX_TIMESTAMP_DELTA_MS: int = 86_400_000  # 24 hours

    async def threads(
        self,
        since: int | None = None,
        existing_thread_ids: set[str] | None = None,
        last_sync: int | None = None,
        on_total=None,
        on_conversation_progress=None,
        store_thread=None,
        on_skip_timestamps=None,
        tail_check=None,
        on_delta_skip=None,
    ) -> AsyncIterator[IngestedThread]:
        """Yield threads that need to be saved.

        Skip known thread if |api_updated_at - db_updated_at| <= MAX_TIMESTAMP_DELTA_MS.
        New threads always fetched.

        On first sha1 match (store_thread returns False): stop fetching details for
        known threads. If tail is verified (oldest thread on last page has matching sha1)
        we stop entirely — no further pagination or timestamp collection needed.
        Otherwise keep paginating to collect api_updated_at timestamps via on_skip_timestamps.
        """
        if existing_thread_ids is None:
            existing_thread_ids = set()

        db_updated_at: dict[str, int | None] = (
            dict(existing_thread_ids)
            if isinstance(existing_thread_ids, dict)
            else {tid: None for tid in existing_thread_ids}
        )

        token, cookies = await self._get_token_via_cdp()
        headers = self._build_headers(token, cookies)

        sha1_stop = False
        tail_verified = False

        async with httpx.AsyncClient(timeout=60, headers=BROWSER_HEADERS) as client:
            limit = 100
            total_fetched = 0

            # --- Fetch page 0 ---
            first_resp = await self._fetch_conversations(client, headers, 0, limit)
            first_data = first_resp.json()
            first_items = first_data.get("items", [])

            if not first_items:
                return

            # --- Check if we have multiple pages ---
            # If we got a full page (100 items), there might be more. Try tail finding if conditions met.
            has_multiple_pages = len(first_items) == limit

            # --- Report total from first page (if available) ---
            if on_total:
                remaining = first_data.get("remaining")
                if remaining is not None:
                    total = len(first_items) + remaining
                    on_total(total)
                elif not has_multiple_pages:
                    # Single page with unknown remaining — just report what we have
                    on_total(len(first_items))

            # --- Tail check: find last page and verify oldest known thread ---
            if tail_check and db_updated_at and has_multiple_pages:
                db_count = len(db_updated_at)
                logger.info("Looking for last page")
                tail_result = await self._find_tail_page(client, headers, db_count, limit)
                if tail_result:
                    tail_offset, tail_items = tail_result
                    total = tail_offset + len(tail_items)
                    logger.info(f"Found {total} conversations")
                    if on_total:
                        on_total(total)

                    # Find oldest known thread on tail page and verify sha1 (if tail_check provided)
                    if tail_check:
                        for conv in reversed(tail_items):
                            conv_id = conv.get("id")
                            if not conv_id:
                                continue
                            thread_id = f"chatgpt:{conv_id}"
                            if thread_id in db_updated_at:
                                logger.debug(f"Tail check: fetching {conv_id}")
                                thread = await self._fetch_thread(
                                    client, conv, headers, total_fetched=total_fetched
                                )
                                if thread:
                                    total_fetched += 1
                                    if tail_check(thread):
                                        tail_verified = True
                                        logger.info(f"Tail verified: {conv_id} sha1 matches — history is complete")
                                break
                else:
                    logger.warning("Could not determine total (tail search failed), proceeding with pagination")

            # --- Main pagination loop, starting with already-fetched page 0 ---
            prefetched = [(0, first_items)]
            next_offset = len(first_items)

            while True:
                if prefetched:
                    offset, items = prefetched.pop(0)
                else:
                    resp = await self._fetch_conversations(client, headers, next_offset, limit)
                    data = resp.json()
                    items = data.get("items", [])
                    offset = next_offset
                    if not items:
                        break

                logger.debug(f"Processing conversations {offset + 1}-{offset + len(items)}")

                page_all_old_and_known = True
                page_skip_timestamps: dict[str, int] = {}
                page_delta_skipped = 0

                for conv in items:
                    conv_id = conv.get("id")
                    if not conv_id:
                        continue
                    thread_id = f"chatgpt:{conv_id}"
                    updated_at = _parse_timestamp(
                        conv.get("update_time") or conv.get("create_time")
                    )

                    if thread_id in db_updated_at:
                        db_ts = db_updated_at.get(thread_id)
                        if (
                            updated_at is not None
                            and db_ts is not None
                            and abs(updated_at - db_ts) <= self.MAX_TIMESTAMP_DELTA_MS
                        ):
                            page_delta_skipped += 1
                            continue

                        if sha1_stop:
                            if updated_at is not None:
                                page_skip_timestamps[thread_id] = updated_at
                            continue

                        logger.debug(
                            f"Conversation {conv_id} needs re-fetch "
                            f"(api={updated_at} db={db_ts})"
                        )
                    else:
                        page_all_old_and_known = False

                    page_all_old_and_known = False

                    thread = await self._fetch_thread(
                        client,
                        conv,
                        headers,
                        on_conversation_progress=on_conversation_progress,
                        total_fetched=total_fetched,
                    )
                    if thread:
                        total_fetched += 1
                        if store_thread:
                            saved = store_thread(thread)
                            if saved is False and not sha1_stop:
                                sha1_stop = True
                                logger.info(
                                    f"sha1 match on {conv_id} — skipping detail fetches for remaining known threads"
                                )
                        yield thread

                if on_delta_skip and page_delta_skipped > 0:
                    on_delta_skip(page_delta_skipped)

                if on_skip_timestamps and page_skip_timestamps and not (sha1_stop and tail_verified):
                    on_skip_timestamps(page_skip_timestamps)

                next_offset = offset + len(items)

                if sha1_stop and tail_verified:
                    logger.info("sha1 stop + tail verified — stopping pagination")
                    break

                if len(items) < limit and not sha1_stop:
                    break

                if not sha1_stop and page_all_old_and_known:
                    logger.info("All conversations already in database, stopping")
                    break

    async def _find_tail_page(
        self, client: httpx.AsyncClient, headers: dict, db_count: int, limit: int = 100
    ) -> tuple[int, list[dict]] | None:
        """Find the last page by walking from estimated position.

        Returns (offset, items) for the last non-empty page, or None if db is empty or API fetch fails.

        Algorithm:
        - Start at estimated position: ((db_count - 1) // limit) * limit
        - If 0 items: walk back (deletion case) until items found
        - If < limit items: this is the last page
        - If limit items: walk forward (new convs case) until partial or 0
        """
        if db_count == 0:
            return None

        estimated_offset = ((db_count - 1) // limit) * limit
        offset = estimated_offset
        direction = None  # "forward", "back", or None if at estimate

        try:
            while True:
                resp = await self._fetch_conversations(client, headers, offset, limit)
                data = resp.json()
                items = data.get("items", [])

                if len(items) == 0:
                    # Empty result: if we came from estimate, walk back; otherwise invalid state
                    if direction != "back":
                        direction = "back"
                        if offset >= limit:
                            offset -= limit
                            continue
                    return None  # walked back to 0 with no results

                if len(items) < limit:
                    # Partial page: this is the last page
                    logger.debug(f"Tail page found at offset {offset} with {len(items)} items")
                    return (offset, items)

                # Full page: check if we should walk forward
                if direction == "forward":
                    # Already walking forward, continue
                    offset += limit
                    continue
                elif direction == "back":
                    # Walked back and found full page: this is the last page (next is 0)
                    logger.debug(f"Tail page found at offset {offset} with {len(items)} items (stepped back)")
                    return (offset, items)
                else:
                    # At estimate with full page: walk forward to see if there are more
                    direction = "forward"
                    offset += limit
                    continue
        except Exception as e:
            logger.warning(f"Tail page search failed: {e}")
            return None

    async def _fetch_conversations(
        self, client: httpx.AsyncClient, headers: dict, offset: int, limit: int
    ) -> httpx.Response:
        """Fetch a page of conversations (not rate limited)."""
        resp = await client.get(
            f"{API_BASE}/conversations",
            params={"offset": offset, "limit": limit},
            headers=headers,
        )

        if resp.status_code == 401:
            raise PermissionError("401 - re-auth required")
        if resp.status_code == 403:
            raise PermissionError("403 - re-auth required")

        resp.raise_for_status()
        return resp

    def _load_stored_token(self) -> tuple[str, dict[str, str]] | None:
        """Load access token and cookies from stored auth file if token is still valid."""
        path = auth_path(self.source_id)
        if not path.exists():
            return None

        try:
            state = json.loads(path.read_text())
        except Exception:
            return None

        token = state.get("access_token")
        if not token:
            return None

        # Decode JWT exp without a library (header.payload.signature)
        exp_days = None
        try:
            parts = token.split(".")
            if len(parts) == 3:
                payload_b64 = parts[1]
                # Add padding
                payload_b64 += "=" * (4 - len(payload_b64) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                exp = payload.get("exp")
                if exp:
                    exp_days = max(0, (exp - time.time()) / 86400)
                    if time.time() > exp - 60:
                        logger.warning("Stored token expired, will fetch fresh auth")
                        return None
        except Exception:
            pass  # Can't decode exp — treat token as valid and let the API reject if needed

        cookies = {c["name"]: c["value"] for c in state.get("cookies", [])}
        if exp_days is not None:
            logger.info(f"Using stored access token (expires in {exp_days:.0f} days)")
        else:
            logger.info("Using stored access token")
        return token, cookies

    async def _get_token_via_cdp(self) -> tuple[str, dict[str, str]]:
        """Get access token and cookies — tries stored auth, then browser cookies, then CDP."""
        stored = self._load_stored_token()
        if stored:
            return stored

        if self._auth_mode == "cookies":
            token = await self._try_browser_cookies_token()
            if token:
                return token
            raise RuntimeError(
                "No valid stored token and browser cookie extraction failed. "
                "Set ingestors.chatgpt.mode = \"cdp\" to use CDP, or login in the configured browser."
            )

        return await self._get_token_via_cdp_fallback()

    async def _try_browser_cookies_token(self) -> tuple[str, dict[str, str]] | None:
        """Try extracting cookies from Waterfox/Firefox and fetching access token via API."""
        try:
            browser_cookies = extract_firefox_cookies(
                browser_dir=self._browser_dir,
                domains=("chatgpt.com", "openai.com"),
            )
        except FileNotFoundError as e:
            logger.warning(f"Browser cookie extraction failed: {e}")
            return None

        cookie_header = cookie_header_for_url(browser_cookies, f"{LOGIN_URL}/api/auth/session")
        if not cookie_header:
            logger.warning("No ChatGPT cookies found for configured browser")
            return None

        cookies = cookies_to_dict(browser_cookies)

        logger.info("Fetching access token from /api/auth/session using browser cookies...")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{LOGIN_URL}/api/auth/session",
                    headers={
                        **BROWSER_HEADERS,
                        "cookie": cookie_header,
                        "referer": LOGIN_URL,
                    },
                    follow_redirects=True,
                )
            if resp.status_code != 200:
                logger.warning(f"Session API returned {resp.status_code}, cannot fetch token")
                return None

            data = resp.json()
            token = data.get("accessToken")
            if not token:
                logger.warning("Session API returned no accessToken")
                return None

            try:
                AUTH_DIR.mkdir(parents=True, exist_ok=True)
                path = auth_path(self.source_id)
                existing = {}
                if path.exists():
                    existing = json.loads(path.read_text())
                existing["access_token"] = token
                existing["cookies"] = browser_cookies
                path.write_text(json.dumps(existing))
                logger.info("Saved fresh token from browser cookies")
            except Exception as e:
                logger.warning(f"Failed to save token: {e}")

            return token, cookies
        except Exception as e:
            logger.warning(f"Failed to fetch token via browser cookies: {e}")
            return None

    async def _get_token_via_cdp_fallback(self) -> tuple[str, dict[str, str]]:
        """Fall back to Chrome CDP for token/cookie extraction."""
        logger.warning("Fetching via Chrome CDP...")

        from playwright.async_api import async_playwright

        chrome_proc = None
        user_data_dir = None
        cdp_port = None

        for port in [CDP_PORT, 9444, 9555]:
            try:
                resp = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=1)
                if resp.status == 200:
                    cdp_port = port
                    break
            except Exception:
                continue

        if not cdp_port:
            logger.info("No Chrome with CDP found, launching Chrome...")
            chrome_proc, cdp_port = _launch_chrome_with_cdp()
            user_data_dir = (
                Path(chrome_proc.args[-1].split("=")[1])
                if "--user-data-dir=" in " ".join(chrome_proc.args)
                else None
            )

        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
                ctx = browser.contexts[0]

                page = None
                for pg in ctx.pages:
                    if "chatgpt.com" in pg.url:
                        page = pg
                        break

                if not page:
                    page = await ctx.new_page()
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

                if "chatgpt.com" not in page.url:
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

                logger.info("Waiting for login... Please login at chatgpt.com if needed")
                for _ in range(300):
                    try:
                        result = await page.evaluate("""async () => {
                            const resp = await fetch('/api/auth/session', {credentials: 'include'});
                            if (resp.ok) {
                                const data = await resp.json();
                                return data.accessToken || null;
                            }
                            return null;
                        }""")
                        if result:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError("Login timeout - please login at chatgpt.com")

                cookies = await ctx.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}

                await browser.close()

                try:
                    path = auth_path(self.source_id)
                    existing = {}
                    if path.exists():
                        existing = json.loads(path.read_text())
                    existing["access_token"] = result
                    existing["cookies"] = [{"name": k, "value": v} for k, v in cookie_dict.items()]
                    path.write_text(json.dumps(existing))
                    logger.info("Saved fresh token to auth file")
                except Exception as e:
                    logger.warning(f"Failed to save token: {e}")

                return result, cookie_dict
        finally:
            _cleanup_chrome_proc(chrome_proc, user_data_dir)

    async def _request_with_message_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict | None = None,
        total_fetched: int = 0,
    ) -> httpx.Response:
        """Fetch a message thread with rate limiting and retry."""
        max_retries = 10

        for attempt in range(max_retries):
            # Apply rate limit delay before request
            delay = self._message_limiter.get_and_apply_delay()
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                resp = await client.get(url, headers=headers)
                self._message_limiter.update_request_time()

                if resp.status_code == 429:
                    wait_time = self._message_limiter.record_429()
                    logger.warning(f"Rate limited, waiting {wait_time:.0f}s")
                    await asyncio.sleep(wait_time)
                    continue

                if resp.status_code >= 500:
                    logger.warning(f"Server error {resp.status_code}, retrying in 5s...")
                    await asyncio.sleep(5)
                    continue

                if resp.status_code == 401 or resp.status_code == 403:
                    raise PermissionError(f"{resp.status_code} - re-auth required")

                # Success
                self._message_limiter.record_success()
                return resp

            except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
                logger.warning(f"Network error: {e}. Retrying in 5s...")
                await asyncio.sleep(5)
                continue

        raise RuntimeError(f"Failed to fetch {url} after {max_retries} retries")

    async def _fetch_thread(
        self,
        client: httpx.AsyncClient,
        conv: dict,
        headers: dict,
        on_conversation_progress=None,
        total_fetched: int = 0,
    ) -> IngestedThread | None:
        conv_id = conv.get("id")
        if not conv_id:
            return None

        resp = await self._request_with_message_with_retry(
            client,
            f"{API_BASE}/conversation/{conv_id}",
            headers=headers,
            total_fetched=total_fetched,
        )

        data = resp.json()
        thread_id = f"chatgpt:{conv_id}"
        messages: list[IngestedMessage] = []

        mapping = data.get("mapping", {})
        total_nodes = len(mapping)
        processed = 0

        for node_id, node in mapping.items():
            msg = node.get("message")
            if not msg:
                processed += 1
                if on_conversation_progress and total_nodes > 50:
                    on_conversation_progress(processed, total_nodes)
                continue

            role = msg.get("author", {}).get("role", "")
            if role not in ("user", "assistant"):
                processed += 1
                if on_conversation_progress and total_nodes > 50:
                    on_conversation_progress(processed, total_nodes)
                continue

            content = _extract_message_text(msg)
            if not content.strip():
                processed += 1
                if on_conversation_progress and total_nodes > 50:
                    on_conversation_progress(processed, total_nodes)
                continue

            ts = _parse_timestamp(msg.get("create_time"))
            msg_id = msg.get("id", node_id)

            metadata: dict = {}
            model = msg.get("model") or conv.get("model")
            if model:
                metadata["model"] = model

            messages.append(
                IngestedMessage(
                    id=f"chatgpt:{msg_id}",
                    thread_id=thread_id,
                    role=role,
                    content=content,
                    created_at=ts,
                    metadata=metadata,
                )
            )

        if not messages:
            return None

        return IngestedThread(
            id=thread_id,
            source_id="chatgpt",
            title=conv.get("title"),
            created_at=_parse_timestamp(conv.get("create_time")),
            updated_at=_parse_timestamp(conv.get("update_time")),
            messages=messages,
        )


async def _login() -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    out = auth_path("chatgpt")

    async def detect_login(ctx, page):
        """Detect ChatGPT login by checking for access token."""
        for i in range(300):
            url = page.url
            if "login" in url.lower():
                if i % 10 == 0:
                    logger.debug("Still on login page...")
                await asyncio.sleep(1)
                continue

            cookies = await ctx.cookies()
            cookie_names = {c["name"] for c in cookies}
            auth_cookies = cookie_names & {
                "auth_token",
                "access_token",
                "__Secure-next-auth.session-token",
                "csrf_token",
            }
            if auth_cookies:
                logger.info(f"Login detected via cookies: {auth_cookies}")
                return True

            try:
                result = await page.evaluate("""async () => {
                    try {
                        const resp = await fetch('/api/auth/session', {credentials: 'include'});
                        const data = await resp.json();
                        return {ok: resp.ok, hasToken: !!data.accessToken};
                    } catch(e) {
                        return {ok: false, error: e.message};
                    }
                }""")
                if result.get("hasToken"):
                    logger.info("Login detected!")
                    return True
                if i % 10 == 0:
                    logger.debug(f"Checking login... URL: {url[:60]}")
            except Exception as e:
                if i % 10 == 0:
                    logger.warning(f"Error checking login: {e}")

            await asyncio.sleep(1)
        logger.warning("Login timeout")
        return False

    await login_with_detection("chatgpt", LOGIN_URL, detect_login, out)


def _parse_timestamp(ts) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts * 1000)
    if isinstance(ts, str):
        try:
            if ts.replace(".", "").replace("-", "").replace("+", "").isdigit():
                return int(float(ts) * 1000)
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def _extract_message_text(msg: dict) -> str:
    parts: list[str] = []
    content = msg.get("content", {})
    for part in content.get("parts", []):
        if isinstance(part, str) and part.strip():
            parts.append(part)
        elif isinstance(part, dict):
            if "text" in part:
                parts.append(part["text"])
            elif part.get("content_type") == "image_asset_pointer":
                parts.append("[Image]")
    return "\n\n".join(p for p in parts if p.strip())
