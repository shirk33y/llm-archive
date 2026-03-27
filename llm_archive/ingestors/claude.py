from __future__ import annotations
import asyncio
import time
from typing import AsyncIterator

import httpx

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedThread

LOGIN_URL = "https://claude.ai"
API_BASE = "https://claude.ai/api"
RATE_LIMIT_DELAY = 1.0  # seconds between requests

BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://claude.ai/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "anthropic-client-platform": "web_claude_ai",
}


class ClaudeIngestor(BaseIngestor):
    source_id = "claude"

    def __init__(self):
        self._cookies: dict[str, str] = {}
        self._org_id: str | None = None

    async def requires_auth(self) -> bool:
        return True

    async def init(self, **kwargs) -> None:
        from llm_archive.auth.playwright import auth_path, login_headful
        # Skip re-auth if valid session already exists
        if not auth_path("claude").exists() or kwargs.get("reauth"):
            await login_headful("claude", LOGIN_URL)
        self._cookies = {}  # will be loaded on first use

    async def _get_cookies(self) -> dict[str, str]:
        if not self._cookies:
            from llm_archive.auth.playwright import load_cookies
            self._cookies = await load_cookies("claude")
        return self._cookies

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
        await asyncio.sleep(RATE_LIMIT_DELAY)
        cookies = await self._get_cookies()
        resp = await client.get(url, params=params, cookies=cookies)

        if resp.status_code == 401:
            raise PermissionError("401 — re-auth required")

        if resp.status_code == 429:
            wait = 5.0
            for attempt in range(3):
                await asyncio.sleep(wait)
                resp = await client.get(url, params=params, cookies=cookies)
                if resp.status_code != 429:
                    break
                wait *= 2

        resp.raise_for_status()
        return resp.json()

    async def _get_org_id(self, client: httpx.AsyncClient) -> str:
        if self._org_id:
            return self._org_id
        data = await self._get(client, f"{API_BASE}/organizations")
        # data is a list of orgs
        if isinstance(data, list):
            self._org_id = data[0]["uuid"]
        else:
            self._org_id = data["uuid"]
        return self._org_id

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        async with httpx.AsyncClient(timeout=30, headers=BROWSER_HEADERS) as client:
            try:
                org_id = await self._get_org_id(client)
            except PermissionError:
                await self._reauth()
                org_id = await self._get_org_id(client)

            offset = 0
            limit = 50
            while True:
                try:
                    data = await self._get(
                        client,
                        f"{API_BASE}/organizations/{org_id}/chat_conversations",
                        params={"limit": limit, "offset": offset},
                    )
                except PermissionError:
                    await self._reauth()
                    data = await self._get(
                        client,
                        f"{API_BASE}/organizations/{org_id}/chat_conversations",
                        params={"limit": limit, "offset": offset},
                    )

                conversations = data if isinstance(data, list) else data.get("conversations", [])
                if not conversations:
                    break

                for conv in conversations:
                    updated_at = _parse_claude_ts(conv.get("updated_at"))
                    if since and updated_at and updated_at < since:
                        continue

                    thread = await self._fetch_thread(client, org_id, conv)
                    if thread:
                        yield thread

                if len(conversations) < limit:
                    break
                offset += limit

    async def _fetch_thread(
        self, client: httpx.AsyncClient, org_id: str, conv: dict
    ) -> IngestedThread | None:
        conv_id = conv.get("uuid") or conv.get("id")
        if not conv_id:
            return None

        try:
            detail = await self._get(
                client,
                f"{API_BASE}/organizations/{org_id}/chat_conversations/{conv_id}",
            )
        except Exception:
            return None

        thread_id = f"claude:{conv_id}"
        chat_messages = detail.get("chat_messages", [])
        messages: list[IngestedMessage] = []

        for i, msg in enumerate(chat_messages):
            role = msg.get("sender", msg.get("role", ""))
            if role == "human":
                role = "user"
            elif role not in ("user", "assistant"):
                continue

            content = _flatten_claude_content(msg.get("content", msg.get("text", "")))
            if not content.strip():
                continue

            ts = _parse_claude_ts(msg.get("created_at"))
            msg_id = msg.get("uuid", f"{conv_id}:{i}")

            metadata: dict = {}
            model = msg.get("model")
            if model:
                metadata["model"] = model

            messages.append(IngestedMessage(
                id=f"claude:{msg_id}",
                thread_id=thread_id,
                role=role,
                content=content,
                created_at=ts,
                metadata=metadata,
            ))

        if not messages:
            return None

        return IngestedThread(
            id=thread_id,
            source_id="claude",
            title=conv.get("name") or conv.get("title"),
            created_at=_parse_claude_ts(conv.get("created_at")),
            updated_at=_parse_claude_ts(conv.get("updated_at")),
            messages=messages,
        )

    async def _reauth(self) -> None:
        from llm_archive.auth.playwright import login_headful
        print("\n[claude] Session expired — re-authenticating...")
        await login_headful("claude", LOGIN_URL)
        self._cookies = {}


def _parse_claude_ts(ts: str | None) -> int | None:
    if not ts:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _flatten_claude_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    parts.append(f"[Tool: {name}]")
                elif btype == "tool_result":
                    inner = block.get("content", "")
                    parts.append(f"[Tool result] {_flatten_claude_content(inner)[:500]}")
            elif isinstance(block, str):
                parts.append(block)
        return "\n\n".join(p for p in parts if p.strip())
    return str(content)
