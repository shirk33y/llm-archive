from __future__ import annotations
import asyncio
from typing import AsyncIterator

import httpx

from llm_archive.auth.browser_cookies import cookies_to_dict, extract_browser_cookies
from llm_archive.config import VALID_AUTH_MODES, load_config
from llm_archive.ingestors.base import BaseIngestor
from llm_archive.ingestors.web import (
    parse_timestamp,
    should_skip_conversation,
)
from llm_archive.logging import get_logger
from llm_archive.schema import IngestedMessage, IngestedThread

logger = get_logger("claude")

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

    def __init__(
        self,
        auth_mode: str | None = None,
        browser_dir: str | None = None,
        browser_path: str | None = None,
    ):
        config = load_config()
        claude_config = config.ingestor(self.source_id)
        mode = auth_mode or claude_config.mode or "cookies"
        if mode not in VALID_AUTH_MODES:
            valid = ", ".join(sorted(VALID_AUTH_MODES))
            raise ValueError(f"Invalid Claude auth mode: {mode!r}. Expected: {valid}")
        self._cookies: dict[str, str] = {}
        self._org_id: str | None = None
        self._auth_mode = mode
        self._browser = claude_config.browser
        self._profile = claude_config.profile
        self._browser_dir = browser_dir or claude_config.browser_dir or config.browser_dir

    async def requires_auth(self) -> bool:
        return True

    async def init(self, **kwargs) -> None:
        self._cookies = {}

    async def _get_cookies(self) -> dict[str, str]:
        if not self._cookies:
            if self._auth_mode == "cookies":
                browser_cookies = extract_browser_cookies(
                    self._browser,
                    profile=self._profile,
                    browser_dir=self._browser_dir,
                    domains=("claude.ai",),
                )
                self._cookies = cookies_to_dict(browser_cookies)

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
            uuid = data[0]["uuid"]
        else:
            uuid = data["uuid"]
        if not isinstance(uuid, str):
            raise ValueError("Claude organization response missing uuid")
        self._org_id = uuid
        return self._org_id

    async def threads(self, since: int | None = None, existing_thread_ids: set[str] | None = None, on_total = None) -> AsyncIterator[IngestedThread]:
        if existing_thread_ids is None:
            existing_thread_ids = set()

        async with httpx.AsyncClient(timeout=30, headers=BROWSER_HEADERS) as client:
            try:
                org_id = await self._get_org_id(client)
            except PermissionError:
                await self._reauth()
                org_id = await self._get_org_id(client)

            # Use v2 endpoint with eventual consistency for fast single-request fetch
            all_conversations: list[dict] = []
            offset = 0
            limit = 1000
            while True:
                try:
                    data = await self._get(
                        client,
                        f"{API_BASE}/organizations/{org_id}/chat_conversations_v2",
                        params={"limit": limit, "offset": offset, "consistency": "eventual"},
                    )
                except PermissionError:
                    await self._reauth()
                    data = await self._get(
                        client,
                        f"{API_BASE}/organizations/{org_id}/chat_conversations_v2",
                        params={"limit": limit, "offset": offset, "consistency": "eventual"},
                    )

                if isinstance(data, dict):
                    conversations = data.get("data", [])
                    has_more = data.get("has_more", False)
                elif isinstance(data, list):
                    conversations = data
                    has_more = len(conversations) == limit
                else:
                    break

                all_conversations.extend(conversations)

                if not has_more:
                    break
                offset += len(conversations)

            if on_total:
                on_total(len(all_conversations))

            # Sort by updated_at descending to ensure smart sync works correctly
            all_conversations.sort(key=lambda c: parse_timestamp(c.get("updated_at")) or 0, reverse=True)

            for conv in all_conversations:
                conv_id = conv.get("uuid") or conv.get("id")
                if not conv_id:
                    continue
                thread_id = f"claude:{conv_id}"
                updated_at = parse_timestamp(conv.get("updated_at"))

                if should_skip_conversation(thread_id, updated_at, existing_thread_ids):
                    continue

                if (
                    isinstance(existing_thread_ids, dict)
                    and thread_id in existing_thread_ids
                    and existing_thread_ids[thread_id] is not None
                    and updated_at is not None
                    and existing_thread_ids[thread_id] < updated_at
                ):
                    logger.debug(f"Conversation {conv_id} was updated, re-fetching")
                
                if since and updated_at and updated_at < since:
                    continue

                thread = await self._fetch_thread(client, org_id, conv)
                if thread:
                    yield thread

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

            ts = parse_timestamp(msg.get("created_at"))
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
            created_at=parse_timestamp(conv.get("created_at")),
            updated_at=parse_timestamp(conv.get("updated_at")),
            messages=messages,
        )

    async def _reauth(self) -> None:
        logger.warning("Session expired, re-authenticating")
        self._cookies = {}


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
