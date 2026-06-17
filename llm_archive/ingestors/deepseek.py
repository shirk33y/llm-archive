from __future__ import annotations
import json

import httpx

from llm_archive.auth.browser_cookies import (
    cookies_to_dict,
    extract_browser_cookies,
    extract_browser_local_storage_value,
)
from llm_archive.config import VALID_AUTH_MODES, load_config
from llm_archive.ingestors.base import BaseIngestor
from llm_archive.logging import get_logger
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

    def __init__(
        self,
        auth_mode: str | None = None,
        browser_dir: str | None = None,
        browser_path: str | None = None,
    ):
        config = load_config()
        deepseek_config = config.ingestor(self.source_id)
        mode = auth_mode or deepseek_config.mode or "cookies"
        if mode not in VALID_AUTH_MODES:
            valid = ", ".join(sorted(VALID_AUTH_MODES))
            raise ValueError(f"Invalid DeepSeek auth mode: {mode!r}. Expected: {valid}")
        self._token: str | None = None
        self._cookies: dict[str, str] = {}
        self._auth_mode = mode
        self._browser = deepseek_config.browser
        self._profile = deepseek_config.profile
        self._browser_dir = browser_dir or deepseek_config.browser_dir or config.browser_dir
        self._browser_path = browser_path or deepseek_config.browser_path or config.browser_path

    async def requires_auth(self) -> bool:
        return True

    async def init(self, **kwargs) -> None:
        self._token = None
        self._cookies = {}

    async def threads(self, since: int | None = None, existing_thread_ids: set[str] | None = None, on_total = None):
        logger.debug("Acquiring auth token")
        token = await self._get_token()
        logger.debug("Token acquired")
        headers = {
            **BROWSER_HEADERS,
            "authorization": f"Bearer {token}",
        }
        cookies = await self._get_cookies()

        if existing_thread_ids is None:
            existing_thread_ids = set()

        async with httpx.AsyncClient(timeout=30, headers=headers, cookies=cookies) as client:
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
                        if db_updated_at is None or updated_at is None or db_updated_at >= updated_at:
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

        self._token = self._get_token_from_browser_storage()
        logger.debug("Extracted bearer token from browser storage")
        return self._token

    async def _get_cookies(self) -> dict[str, str]:
        if not self._cookies and self._auth_mode == "cookies":
            browser_cookies = extract_browser_cookies(
                self._browser,
                profile=self._profile,
                browser_dir=self._browser_dir,
                domains=("chat.deepseek.com", "deepseek.com"),
            )
            self._cookies = cookies_to_dict(browser_cookies)
        return self._cookies

    def _get_token_from_browser_storage(self) -> str:
        token = extract_browser_local_storage_value(
            "https://chat.deepseek.com",
            "userToken",
            self._browser,
            profile=self._profile,
            browser_dir=self._browser_dir,
        )
        if not token:
            raise FileNotFoundError("No DeepSeek userToken found in browser localStorage")
        try:
            parsed = json.loads(token)
            if isinstance(parsed, dict) and isinstance(parsed.get("value"), str):
                token = parsed["value"]
        except (json.JSONDecodeError, TypeError):
            pass
        if not token:
            raise FileNotFoundError("No DeepSeek userToken found in browser localStorage")
        return token

    async def _reauth(self) -> None:
        self._token = None
        self._cookies = {}

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
