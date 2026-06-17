from __future__ import annotations

import json
import re
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.ingestors.claudecode import _parse_timestamp
from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread

DEFAULT_ROOT = Path.home() / ".cursor" / "projects"
USER_QUERY_RE = re.compile(r"^\s*<user_query>\s*(.*?)\s*</user_query>\s*$", re.DOTALL)


def _clean_user_text(text: str) -> str:
    match = USER_QUERY_RE.match(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _flatten_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content is not None else ""

    chunks: list[str] = []
    for block in content:
        if isinstance(block, str):
            chunks.append(block)
            continue
        if not isinstance(block, dict):
            chunks.append(str(block))
            continue
        text = block.get("text")
        if isinstance(text, str):
            chunks.append(text)
            continue
        nested = block.get("content")
        if nested:
            chunks.append(_flatten_content(nested))
    return "\n\n".join(chunk for chunk in chunks if chunk.strip())


def _assistant_parts(entries: list[dict]) -> tuple[str, list[IngestedPart]]:
    texts = [_flatten_content((entry.get("message") or {}).get("content", "")) for entry in entries]
    texts = [text for text in texts if text.strip()]
    parts: list[IngestedPart] = []
    for index, text in enumerate(texts):
        kind = "text" if index == len(texts) - 1 else "reasoning"
        parts.append(
            IngestedPart(
                kind=kind,
                text=text,
                visible=True,
                searchable=kind == "text",
            )
        )
    return "\n\n".join(texts), parts


def _parse_jsonl(path: Path) -> IngestedThread | None:
    entries: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    except OSError:
        return None

    if not entries:
        return None

    session_id = path.parent.name if path.parent.name != "agent-transcripts" else path.stem
    thread_id = f"cursor:{session_id}"
    messages: list[IngestedMessage] = []
    assistant_entries: list[dict] = []
    created_at: int | None = None
    updated_at: int | None = None

    def flush_assistant() -> None:
        nonlocal created_at, updated_at
        if not assistant_entries:
            return
        content, parts = _assistant_parts(assistant_entries)
        if not content.strip():
            assistant_entries.clear()
            return
        ts = _parse_timestamp(assistant_entries[0].get("timestamp"))
        if ts is not None:
            created_at = ts if created_at is None else created_at
            updated_at = ts
        msg_id = assistant_entries[0].get("id") or assistant_entries[0].get("uuid")
        if not msg_id:
            msg_id = f"{session_id}:assistant:{len(messages)}"
        messages.append(
            IngestedMessage(
                id=f"cursor:{msg_id}",
                thread_id=thread_id,
                role="assistant",
                content=content,
                created_at=ts,
                parts=parts,
                raw={"entries": assistant_entries.copy()},
            )
        )
        assistant_entries.clear()

    for entry in entries:
        message = entry.get("message") or {}
        role = message.get("role") or entry.get("role")
        if role == "assistant":
            assistant_entries.append(entry)
            continue
        if role != "user":
            continue

        flush_assistant()
        content = _clean_user_text(_flatten_content(message.get("content", "")))
        if not content:
            continue
        ts = _parse_timestamp(entry.get("timestamp"))
        if ts is not None:
            created_at = ts if created_at is None else created_at
            updated_at = ts
        msg_id = entry.get("id") or entry.get("uuid") or f"{session_id}:user:{len(messages)}"
        messages.append(
            IngestedMessage(
                id=f"cursor:{msg_id}",
                thread_id=thread_id,
                role="user",
                content=content,
                created_at=ts,
                parts=[IngestedPart(kind="text", text=content)],
                raw=entry,
            )
        )

    flush_assistant()

    if not messages:
        return None

    first_user = next((message for message in messages if message.role == "user"), None)
    title = first_user.content[:80].split("\n")[0].strip() if first_user else session_id
    return IngestedThread(
        id=thread_id,
        source_id="cursor",
        title=title or session_id,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
    )


class CursorIngestor(BaseIngestor):
    source_id = "cursor"

    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = root

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        if kwargs.get("path"):
            self.root = Path(kwargs["path"]).expanduser()

    async def count_threads(self, since: int | None = None) -> int:
        return sum(1 for _ in self._paths(since))

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        for path in self._paths(since):
            thread = _parse_jsonl(path)
            if thread:
                yield thread

    def _paths(self, since: int | None) -> list[Path]:
        if not self.root.exists():
            return []
        paths = sorted(self.root.glob("*/agent-transcripts/**/*.jsonl"))
        if since is None:
            return paths
        return [path for path in paths if int(path.stat().st_mtime * 1000) >= since]
