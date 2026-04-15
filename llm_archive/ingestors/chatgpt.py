from __future__ import annotations
import asyncio
from datetime import datetime
from typing import AsyncIterator

import httpx

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.logging import get_logger
from llm_archive.schema import IngestedMessage, IngestedThread

logger = get_logger("chatgpt")

LOGIN_URL = "https://chatgpt.com"
API_BASE = "https://chatgpt.com/backend-api"
RATE_LIMIT_DELAY = 1.0

BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "accept": "application/json",
    "content-type": "application/json",
}


class ChatGPTIngestor(BaseIngestor):
    source_id = "chatgpt"

    async def requires_auth(self) -> bool:
        return True

    async def init(self, **kwargs) -> None:
        from llm_archive.auth.playwright import auth_path

        if not auth_path(self.source_id).exists() or kwargs.get("reauth"):
            await _login()

    async def _get_cookies(self) -> dict[str, str]:
        from llm_archive.auth.playwright import auth_path

        path = auth_path(self.source_id)
        if not path.exists():
            raise FileNotFoundError(
                "No auth found for 'chatgpt'. Run `llm-archive init chatgpt` first."
            )

        import json

        state = json.loads(path.read_text())
        cookies = {c["name"]: c["value"] for c in state.get("cookies", [])}
        return cookies

    async def threads(
        self, since: int | None = None, existing_thread_ids: set[str] | None = None, on_total=None
    ) -> AsyncIterator[IngestedThread]:
        if existing_thread_ids is None:
            existing_thread_ids = set()

        cookies = await self._get_cookies()
        client = httpx.AsyncClient(timeout=30, cookies=cookies, headers=BROWSER_HEADERS)

        try:
            all_conversations = await self._fetch_conversations(client)
        except PermissionError:
            await client.aclose()
            await self._reauth()
            cookies = await self._get_cookies()
            client = httpx.AsyncClient(timeout=30, cookies=cookies, headers=BROWSER_HEADERS)
            all_conversations = await self._fetch_conversations(client)

        try:
            if on_total:
                on_total(len(all_conversations))

            for conv in all_conversations:
                conv_id = conv.get("id")
                if not conv_id:
                    continue
                thread_id = f"chatgpt:{conv_id}"
                updated_at = _parse_timestamp(conv.get("update_time") or conv.get("create_time"))

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

                thread = await self._fetch_thread(client, conv)
                if thread:
                    yield thread
        finally:
            await client.aclose()

    async def _fetch_conversations(self, client: httpx.AsyncClient) -> list[dict]:
        all_conversations: list[dict] = []
        offset = 0
        limit = 100

        while True:
            resp = await client.get(
                f"{API_BASE}/conversations", params={"offset": offset, "limit": limit}
            )
            if resp.status_code == 401:
                raise PermissionError("401 - re-auth required")
            if resp.status_code == 403:
                raise PermissionError("403 - access denied")
            resp.raise_for_status()

            data = resp.json()
            items = data.get("items", [])
            total = data.get("total", 0)

            if not items:
                break

            all_conversations.extend(items)
            offset += len(items)

            if offset >= total:
                break

            await asyncio.sleep(RATE_LIMIT_DELAY)

        all_conversations.sort(
            key=lambda c: _parse_timestamp(c.get("update_time") or c.get("create_time")) or 0,
            reverse=True,
        )
        return all_conversations

    async def _fetch_thread(self, client: httpx.AsyncClient, conv: dict) -> IngestedThread | None:
        conv_id = conv.get("id")
        if not conv_id:
            return None

        resp = await client.get(f"{API_BASE}/conversation/{conv_id}")
        if resp.status_code == 401:
            raise PermissionError("401 - re-auth required")
        if not resp.is_success:
            return None

        data = resp.json()
        thread_id = f"chatgpt:{conv_id}"
        messages: list[IngestedMessage] = []

        for node_id, node in data.get("mapping", {}).items():
            msg = node.get("message")
            if not msg:
                continue

            role = msg.get("author", {}).get("role", "")
            if role not in ("user", "assistant"):
                continue

            content = _extract_message_text(msg)
            if not content.strip():
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

    async def _reauth(self) -> None:
        logger.warning("Session expired, re-authenticating")
        await _login()
        self._token = None


async def _login() -> None:
    from llm_archive.auth.playwright import AUTH_DIR, auth_path, login_with_detection

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    out = auth_path("chatgpt")

    async def detect_login(ctx, page):
        """Detect ChatGPT login by checking for access token or cookies."""
        for i in range(300):
            url = page.url
            if "login" in url.lower():
                if i % 10 == 0:
                    logger.info(f"Still on login page: {url}")
                await asyncio.sleep(1)
                continue

            # Check cookies for auth evidence
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

            # Check for access token via API
            try:
                result = await page.evaluate("""async () => {
                    try {
                        const resp = await fetch('/api/auth/session', {credentials: 'include'});
                        const data = await resp.json();
                        return {ok: resp.ok, status: resp.status, hasToken: !!data.accessToken, token: data.accessToken ? data.accessToken.substring(0, 20) + '...' : null};
                    } catch(e) {
                        return {ok: false, error: e.message};
                    }
                }""")
                if result.get("hasToken"):
                    logger.info(f"Login detected! Token: {result.get('token')}")
                    return True
                if i % 10 == 0:
                    logger.info(f"Checking login... URL: {url[:60]}, resp: {result}")
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
