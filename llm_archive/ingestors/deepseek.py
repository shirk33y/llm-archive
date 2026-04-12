from __future__ import annotations
import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedThread

LOGIN_URL = "https://chat.deepseek.com/"
API_BASE = "https://chat.deepseek.com/api/v0"
BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://chat.deepseek.com/",
    "x-client-platform": "web",
}


class DeepseekIngestor(BaseIngestor):
    source_id = "deepseek"

    def __init__(self):
        self._token: str | None = None

    async def requires_auth(self) -> bool:
        return True

    async def init(self, **kwargs) -> None:
        from llm_archive.auth.playwright import auth_path
        print("[deepseek] checking auth state")
        if not auth_path(self.source_id).exists() or kwargs.get("reauth"):
            print("[deepseek] auth missing or reauth requested — starting browser login")
            await _login()
            print("[deepseek] login completed and auth state saved")
        self._token = None

    async def threads(self, since: int | None = None):
        print("[deepseek] acquiring auth token")
        token = await self._get_token()
        print("[deepseek] token acquired")
        headers = {
            **BROWSER_HEADERS,
            "authorization": f"Bearer {token}",
        }

        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            sessions = await self._fetch_sessions(client)
            print(f"[deepseek] found {len(sessions)} conversations")
            for sess in sessions:
                updated_at = _parse_ts(sess.get("updated_at"))
                if since and updated_at and updated_at < since:
                    continue
                thread = await self._fetch_thread(client, sess)
                if thread:
                    yield thread

    async def count_threads(self, since: int | None = None) -> int:
        token = await self._get_token()
        headers = {
            **BROWSER_HEADERS,
            "authorization": f"Bearer {token}",
        }
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            sessions = await self._fetch_sessions(client)
        if since is None:
            return len(sessions)
        return sum(
            1
            for sess in sessions
            if not (since and _parse_ts(sess.get("updated_at")) and _parse_ts(sess.get("updated_at")) < since)
        )

    async def _fetch_sessions(self, client: httpx.AsyncClient) -> list[dict]:
        print("[deepseek] fetching conversation list")
        sessions: list[dict] = []
        seen: set[str] = set()
        cursor: int | None = None

        while True:
            params = {"lte_cursor.pinned": "false"}
            if cursor is not None:
                params["lte_cursor.seq_id"] = str(cursor)
            resp = await client.get(f"{API_BASE}/chat_session/fetch_page", params=params)
            if resp.status_code == 401:
                print("[deepseek] conversation list returned 401 — reauth required")
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

            last = next((sess for sess in reversed(page) if isinstance(sess.get("seq_id"), int | float)), None)
            if not last:
                break

            next_cursor = int(last["seq_id"]) - 1
            if cursor is not None and next_cursor >= cursor:
                break
            if not fresh:
                break
            cursor = next_cursor

        if not sessions:
            print("[deepseek] no conversations returned by API")
        return sessions

    async def _fetch_thread(self, client: httpx.AsyncClient, sess: dict) -> IngestedThread | None:
        sess_id = sess.get("id")
        if not sess_id:
            return None

        print(f"[deepseek] fetching conversation {sess_id}")
        resp = await client.get(f"{API_BASE}/chat/history_messages", params={"chat_session_id": sess_id})
        if resp.status_code == 401:
            print(f"[deepseek] conversation {sess_id} returned 401 — reauth required")
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
            print(f"[deepseek] conversation {sess_id} had no importable messages")
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
        from playwright.async_api import async_playwright

        path = auth_path(self.source_id)
        if not path.exists():
            raise FileNotFoundError("No auth found for 'deepseek'. Run `llm-archive sync deepseek` first.")

        async with async_playwright() as p:
            print("[deepseek] launching headless browser to extract token")
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(storage_state=str(path))
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
            self._token = await asyncio.wait_for(fut, timeout=20)
            await browser.close()
            print("[deepseek] extracted bearer token from browser session")

        return self._token

    async def _reauth(self) -> None:
        self._token = None
        print("[deepseek] reauthenticating")
        await _login()


async def _login() -> None:
    from playwright.async_api import async_playwright
    from llm_archive.auth.playwright import AUTH_DIR, _find_chrome, auth_path

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    out = auth_path("deepseek")
    chrome = _find_chrome()
    args = chrome if isinstance(chrome, list) else [chrome]
    profile = Path(tempfile.mkdtemp(prefix="llm-archive-deepseek-", dir=str(AUTH_DIR)))
    proc = subprocess.Popen([
        *args,
        "--remote-debugging-port=9222",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--ozone-platform=x11",
        f"--user-data-dir={profile}",
        LOGIN_URL,
    ])
    time.sleep(2)

    async with async_playwright() as p:
        print("[deepseek] connecting to Chrome DevTools")
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
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
            fut.set_result(None)
            await ctx.storage_state(path=str(out))

        page.on("request", on_request)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("Log in to DeepSeek in the opened browser. Waiting up to 5 minutes...")
        await asyncio.wait_for(fut, timeout=300)
        print("[deepseek] login request detected")
        await browser.close()

    proc.terminate()


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
