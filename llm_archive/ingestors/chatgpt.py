"""
ChatGPT web ingestor.

Authentication: Uses Chrome CDP to fetch fresh access_token from browser session.
Headless browsers trigger Cloudflare CAPTCHA, so we connect to an existing Chrome
instance via remote debugging port (9333 by default, 9222 is used by Windsurf).

Important: Chrome flatpak ignores CDP flags when using the default profile.
For flatpak Chrome, use an isolated temp profile:
    flatpak run com.google.Chrome --remote-debugging-port=9333 --user-dir=/tmp/chatgpt-chrome

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
from datetime import datetime
from typing import AsyncIterator

import httpx

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.logging import get_logger
from llm_archive.ratelimit import MessageRateLimiter
from llm_archive.schema import IngestedMessage, IngestedThread

logger = get_logger("chatgpt")

LOGIN_URL = "https://chatgpt.com"
API_BASE = "https://chatgpt.com/backend-api"

BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "accept": "application/json",
    "content-type": "application/json",
}


class ChatGPTIngestor(BaseIngestor):
    source_id = "chatgpt"

    def __init__(self):
        self._message_limiter = MessageRateLimiter()

    async def requires_auth(self) -> bool:
        return True

    async def init(self, **kwargs) -> None:
        from llm_archive.auth.playwright import auth_path

        if not auth_path(self.source_id).exists() or kwargs.get("reauth"):
            await _login()

    def _build_headers(self, token: str, cookies: dict[str, str]) -> dict[str, str]:
        """Build request headers with browser-like headers."""
        headers = dict(BROWSER_HEADERS)
        headers["authorization"] = f"Bearer {token}"
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if "oai-did" in cookies:
            headers["oai-device-id"] = cookies["oai-did"]
        return headers

    async def threads(
        self,
        since: int | None = None,
        existing_thread_ids: set[str] | None = None,
        on_total=None,
        on_conversation_progress=None,
        store_thread=None,
    ) -> AsyncIterator[IngestedThread]:
        if existing_thread_ids is None:
            existing_thread_ids = set()

        token, cookies = await self._get_token_via_cdp()
        headers = self._build_headers(token, cookies)

        async with httpx.AsyncClient(timeout=60, headers=BROWSER_HEADERS) as client:
            offset = 0
            limit = 100
            total_estimate = None
            total_fetched = 0

            while True:
                # Fetch next chunk of conversations (no rate limit)
                resp = await self._fetch_conversations(client, headers, offset, limit)

                data = resp.json()
                items = data.get("items", [])

                if not items:
                    break

                if total_estimate is None:
                    remaining = data.get("remaining")
                    if remaining is not None:
                        total_estimate = offset + len(items) + remaining
                    else:
                        total_estimate = offset + len(items) + (len(items) * 2)
                    if on_total:
                        on_total(total_estimate)

                logger.info(f"Processing conversations {offset + 1}-{offset + len(items)}")

                # Process each conversation in this chunk
                for conv in items:
                    conv_id = conv.get("id")
                    if not conv_id:
                        continue
                    thread_id = f"chatgpt:{conv_id}"
                    updated_at = _parse_timestamp(
                        conv.get("update_time") or conv.get("create_time")
                    )

                    if thread_id in existing_thread_ids:
                        if isinstance(existing_thread_ids, dict):
                            db_updated_at = existing_thread_ids.get(thread_id)
                            if db_updated_at and db_updated_at >= updated_at:
                                continue
                            logger.info(f"Conversation {conv_id} was updated, re-fetching")
                        else:
                            continue

                    if since and updated_at and updated_at < since:
                        continue

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
                            store_thread(thread)
                        yield thread

                offset += len(items)

                if len(items) < limit:
                    break

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

    async def _get_token_via_cdp(self) -> tuple[str, dict[str, str]]:
        """Get access token and cookies by connecting to Chrome via CDP."""
        import urllib.request
        from playwright.async_api import async_playwright

        cdp_port = None
        for port in [9333, 9444, 9555]:
            try:
                resp = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=1)
                if resp.status == 200:
                    cdp_port = port
                    break
            except Exception:
                continue

        if not cdp_port:
            raise RuntimeError(
                "Chrome with CDP not found. Run Chrome with --remote-debugging-port=9333"
            )

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            ctx = browser.contexts[0]

            page = None
            for pg in ctx.pages:
                if "chatgpt.com" in pg.url:
                    page = pg
                    break
            if not page and ctx.pages:
                page = ctx.pages[0]

            if not page:
                await browser.close()
                raise RuntimeError("No ChatGPT page found")

            cookies = await ctx.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            result = await page.evaluate("""async () => {
                const resp = await fetch('/api/auth/session', {credentials: 'include'});
                if (resp.ok) {
                    const data = await resp.json();
                    return data.accessToken || null;
                }
                return null;
            }""")

            await browser.close()

            if not result:
                raise RuntimeError("Failed to get access token from ChatGPT")

            return result, cookie_dict

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
                req_start = datetime.now()
                resp = await client.get(url, headers=headers)
                req_duration = (datetime.now() - req_start).total_seconds()
                self._message_limiter.update_request_time()

                if resp.status_code == 429:
                    wait_time = self._message_limiter.record_429()
                    next_delay = self._message_limiter.get_delay()
                    logger.warning(
                        f"Rate limited! 429 #{self._message_limiter.consecutive_429s}, fetched {total_fetched}, req {req_duration:.1f}s, wait {wait_time:.0f}s, next {next_delay:.0f}s"
                    )
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
    from llm_archive.auth.playwright import AUTH_DIR, auth_path, login_with_detection

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    out = auth_path("chatgpt")

    async def detect_login(ctx, page):
        """Detect ChatGPT login by checking for access token."""
        for i in range(300):
            url = page.url
            if "login" in url.lower():
                if i % 10 == 0:
                    logger.info("Still on login page...")
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
                    logger.info(f"Checking login... URL: {url[:60]}")
            except Exception as e:
                if i % 10 == 0:
                    logger.info(f"Error: {e}")

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
