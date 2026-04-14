from __future__ import annotations
import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.logging import get_logger, retry_async
from llm_archive.schema import IngestedMessage, IngestedThread

logger = get_logger("deepseek")

LOGIN_URL = "https://chat.deepseek.com/"
API_BASE = "https://chat.deepseek.com/api/v0"
BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://chat.deepseek.com/",
    "x-client-platform": "web",
    "x-client-version": "1.8.0",
    "x-client-locale": "en_US",
    "x-client-timezone-offset": "7200",
    "x-app-version": "20241129.1",
}


class DeepseekIngestor(BaseIngestor):
    source_id = "deepseek"

    def __init__(self):
        self._token: str | None = None

    async def requires_auth(self) -> bool:
        return True

    async def init(self, **kwargs) -> None:
        from llm_archive.auth.playwright import auth_path
        logger.info("Checking auth state")
        if not auth_path(self.source_id).exists() or kwargs.get("reauth"):
            logger.info("Auth missing or reauth requested, starting browser login")
            await _login()
            logger.info("Login completed and auth state saved")
        self._token = None

    async def threads(self, since: int | None = None, existing_thread_ids: set[str] | None = None, on_total = None):
        logger.debug("Acquiring auth token")
        token = await self._get_token()
        logger.debug("Token acquired")
        headers = {
            **BROWSER_HEADERS,
            "authorization": f"Bearer {token}",
        }

        if existing_thread_ids is None:
            existing_thread_ids = set()

        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            sessions = await self._fetch_sessions(client)
            logger.debug(f"Found {len(sessions)} conversations")
            if on_total:
                on_total(len(sessions))
            for sess in sessions:
                chat_id = sess.get("id")
                if not chat_id:
                    continue
                thread_id = f"deepseek:{chat_id}"
                updated_at = _parse_ts(sess.get("updated_at"))
                
                # Smart sync: skip conversations already in the DB
                if thread_id in existing_thread_ids:
                    if isinstance(existing_thread_ids, dict):
                        db_updated_at = existing_thread_ids.get(thread_id)
                        if db_updated_at and db_updated_at >= updated_at:
                            continue
                        logger.info(f"Conversation {chat_id} was updated, re-fetching")
                    else:
                        continue
                
                if since and updated_at and updated_at < since:
                    continue
                thread = await self._fetch_thread(client, sess)
                if thread:
                    yield thread

    async def _fetch_sessions(self, client: httpx.AsyncClient) -> list[dict]:
        logger.debug("Fetching conversation list")
        sessions: list[dict] = []
        seen: set[str] = set()
        cursor: float | None = None

        while True:
            params = {"lte_cursor.pinned": "false"}
            if cursor is not None:
                params["lte_cursor.updated_at"] = str(cursor)
            resp = await client.get(f"{API_BASE}/chat_session/fetch_page", params=params)
            if resp.status_code == 401:
                logger.warning("Conversation list returned 401, reauth required")
                await self._reauth()
                return await self._fetch_sessions(client)
            resp.raise_for_status()

            data = resp.json().get("data", {}).get("biz_data", {})
            page = data.get("chat_sessions", [])
            if not page:
                break

            fresh = [sess for sess in page if sess.get("id") and sess["id"] not in seen]
            for sess in fresh:
                seen.add(sess["id"])
            sessions.extend(fresh)

            if not data.get("has_more"):
                break

            last = next((sess for sess in reversed(page) if sess.get("updated_at")), None)
            if not last:
                break

            next_cursor = last["updated_at"]
            if cursor is not None and next_cursor >= cursor:
                break
            if not fresh:
                break
            cursor = next_cursor

        if not sessions:
            logger.warning("No conversations returned by API")
        logger.debug(f"Fetched {len(sessions)} conversations")
        return sessions

    async def _fetch_thread(self, client: httpx.AsyncClient, sess: dict) -> IngestedThread | None:
        sess_id = sess.get("id")
        if not sess_id:
            return None

        logger.debug(f"Fetching conversation {sess_id}")
        resp = await client.get(f"{API_BASE}/chat/history_messages", params={"chat_session_id": sess_id})
        if resp.status_code == 401:
            logger.warning(f"Conversation {sess_id} returned 401, reauth required")
            await self._reauth()
            return await self._fetch_thread(client, sess)
        resp.raise_for_status()

        data = resp.json().get("data", {}).get("biz_data", {})
        chat = data.get("chat_session") or sess
        rows = data.get("chat_messages", [])
        thread_id = f"deepseek:{sess_id}"
        messages = []

        for i, row in enumerate(rows):
            role = _role(row.get("role"))
            content = _message_content(row)
            if not role or not content.strip():
                continue
            messages.append(IngestedMessage(
                id=f"deepseek:{sess_id}:{row.get('message_id', i)}",
                thread_id=thread_id,
                role=role,
                content=content,
                created_at=_parse_ts(row.get("inserted_at")),
                metadata=_metadata(row),
            ))

        if not messages:
            logger.warning(f"Conversation {sess_id} had no importable messages")
            return None

        return IngestedThread(
            id=thread_id,
            source_id=self.source_id,
            title=chat.get("title"),
            created_at=_parse_ts(chat.get("inserted_at")),
            updated_at=_parse_ts(chat.get("updated_at")),
            messages=messages,
        )

    async def _get_token(self) -> str:
        if self._token:
            return self._token

        from llm_archive.auth.playwright import auth_path

        path = auth_path(self.source_id)
        if not path.exists():
            raise FileNotFoundError("No auth found for 'deepseek'. Run `llm-archive sync deepseek' first.")

        # Try to extract bearer token directly from storage state (localStorage)
        import json
        state = json.loads(path.read_text())
        origins = state.get("origins", [])
        
        # Look for bearer token in localStorage
        for origin in origins:
            if origin.get("origin") == "https://chat.deepseek.com":
                for item in origin.get("localStorage", []):
                    if item.get("name") in ("accessToken", "token", "userToken"):
                        token = item.get("value")
                        if token:
                            # Token might be JSON-encoded
                            try:
                                parsed = json.loads(token)
                                if isinstance(parsed, dict) and "value" in parsed:
                                    token = parsed["value"]
                            except (json.JSONDecodeError, TypeError):
                                pass
                            self._token = token
                            logger.debug("Extracted bearer token from storage state")
                            return self._token

        # If not found in localStorage, fall back to Chrome extraction
        return await self._get_token_via_chrome(path)

    async def _get_token_via_chrome(self, path: Path) -> str:
        """Fallback: extract bearer token by launching Chrome and intercepting request."""
        from llm_archive.auth.playwright import _find_chrome
        from playwright.async_api import async_playwright

        # Use CDP to connect to Chrome with saved storage state
        chrome = _find_chrome()
        chrome_args = chrome if isinstance(chrome, list) else [chrome]
        chrome_profile = path.parent / "chrome-profile"
        chrome_profile.mkdir(parents=True, exist_ok=True)

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

        import time
        time.sleep(2)

        try:
            async with async_playwright() as p:
                logger.debug("Connecting to Chrome via CDP")
                browser = await p.chromium.connect_over_cdp("http://localhost:9222")
                ctx = browser.contexts[0]
                import json
                state = json.loads(path.read_text())
                await ctx.add_cookies(state.get("cookies", []))
                page = await ctx.new_page()
                fut = asyncio.get_running_loop().create_future()

                async def on_request(req):
                    if "/api/v0/" not in req.url:
                        return
                    auth = req.headers.get("authorization")
                    if auth and auth.startswith("Bearer ") and not fut.done():
                        fut.set_result(auth.removeprefix("Bearer ").strip())

                page.on("request", on_request)
                await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                self._token = await asyncio.wait_for(fut, timeout=120)
                await browser.close()
                logger.debug("Extracted bearer token from browser session")
        finally:
            proc.terminate()

        return self._token

    async def _reauth(self) -> None:
        self._token = None
        logger.info("Reauthenticating")
        await _login()


async def _login() -> None:
    from llm_archive.auth.playwright import AUTH_DIR, auth_path, login_with_detection

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    out = auth_path("deepseek")

    async def detect_login(ctx, page):
        """Detect DeepSeek login by checking for Bearer token in API requests."""
        fut = asyncio.get_running_loop().create_future()
        done = False

        async def on_request(req):
            nonlocal done
            if done or fut.done():
                return
            if "/api/v0/users/current" not in req.url and "/api/v0/chat_session/fetch_page" not in req.url:
                return
            auth = req.headers.get("authorization")
            if not auth or not auth.startswith("Bearer "):
                return
            done = True
            fut.set_result(True)

        page.on("request", on_request)
        return await asyncio.wait_for(fut, timeout=300)

    await login_with_detection("deepseek", LOGIN_URL, detect_login, out)


def _role(role: str | None) -> str | None:
    if role == "USER":
        return "user"
    if role == "ASSISTANT":
        return "assistant"
    return None


def _parse_ts(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value) * 1000)
    except Exception:
        return None


def _metadata(row: dict) -> dict:
    meta = {}
    if row.get("model"):
        meta["model"] = row["model"]
    if row.get("thinking_enabled") is not None:
        meta["thinking_enabled"] = row["thinking_enabled"]
    if row.get("search_enabled") is not None:
        meta["search_enabled"] = row["search_enabled"]
    if row.get("accumulated_token_usage") is not None:
        meta["tokens"] = row["accumulated_token_usage"]
    return meta


def _message_content(row: dict) -> str:
    parts: list[str] = []
    content = row.get("content")
    if isinstance(content, str) and content.strip():
        parts.append(content)
    thinking = row.get("thinking_content")
    if isinstance(thinking, str) and thinking.strip():
        parts.append(f"[Thinking]\n{thinking}")
    results = row.get("search_results")
    if isinstance(results, list) and results:
        text = "\n".join(
            "\n".join(part for part in [item.get("title"), item.get("url")] if part)
            for item in results[:10]
        )
        if text:
            parts.append(f"[Search results]\n{text}")
    if parts:
        return "\n\n".join(parts)
    return _flatten_fragments(row.get("fragments", []))


def _flatten_fragments(fragments: list[dict]) -> str:
    parts: list[str] = []

    for frag in fragments:
        kind = frag.get("type")
        content = frag.get("content")

        if kind == "REQUEST" and isinstance(content, str) and content.strip():
            parts.append(content)
            continue

        if kind == "TEXT" and isinstance(content, str) and content.strip():
            parts.append(content)
            continue

        if kind == "THINK" and isinstance(content, str) and content.strip():
            parts.append(f"[Thinking]\n{content}")
            continue

        queries = frag.get("queries")
        if isinstance(queries, list) and queries:
            q = "\n".join(item.get("query", "") for item in queries if item.get("query"))
            if q:
                parts.append(f"[Search]\n{q}")

        results = frag.get("results")
        if isinstance(results, list) and results:
            text = "\n".join(
                "\n".join(part for part in [item.get("title"), item.get("url")] if part)
                for item in results[:10]
            )
            if text:
                parts.append(f"[Search results]\n{text}")

        if isinstance(content, str) and content.strip():
            parts.append(content)
            continue

        if isinstance(content, dict) and content:
            parts.append(json.dumps(content, ensure_ascii=False))

    return "\n\n".join(part for part in parts if part.strip())
