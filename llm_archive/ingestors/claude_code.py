from __future__ import annotations
import json
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedThread

DEFAULT_ROOT = Path.home() / ".claude" / "projects"


def _parse_timestamp(ts: str | None) -> int | None:
    if not ts:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _flatten_content(content) -> str:
    """Flatten Claude Code message content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type", "")
        if btype == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
        elif btype == "thinking":
            thinking = block.get("thinking", "")
            if thinking:
                parts.append(f"[Thinking]\n{thinking}")
        elif btype == "tool_use":
            name = block.get("name", "tool")
            inp = block.get("input", {})
            cmd = inp.get("command", inp.get("code", json.dumps(inp, ensure_ascii=False)[:200]))
            parts.append(f"[Tool: {name}]\n{cmd}")
        elif btype == "tool_result":
            content_inner = block.get("content", "")
            text = _flatten_content(content_inner)
            parts.append(f"[Tool result]\n{text}")
        else:
            # unknown block type — try to extract any text-like field
            for key in ("text", "content", "output"):
                val = block.get(key)
                if val and isinstance(val, str):
                    parts.append(val)
                    break
    return "\n\n".join(p for p in parts if p.strip())


def _parse_jsonl(path: Path) -> IngestedThread | None:
    lines = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        return None

    if not lines:
        return None

    # Extract sessionId from first message that has it
    session_id = None
    for entry in lines:
        session_id = entry.get("sessionId")
        if session_id:
            break
    if not session_id:
        session_id = path.stem

    thread_id = f"claude_code:{session_id}"
    messages: list[IngestedMessage] = []
    created_at = None
    updated_at = None

    for entry in lines:
        etype = entry.get("type")
        # Skip non-message entries
        if etype in ("queue-operation", "file-history-snapshot"):
            continue

        msg_data = entry.get("message")
        if not msg_data:
            continue

        role = msg_data.get("role")
        if role not in ("user", "assistant"):
            continue

        content_raw = msg_data.get("content", "")
        content = _flatten_content(content_raw)
        if not content.strip():
            continue

        ts_str = entry.get("timestamp")
        ts = _parse_timestamp(ts_str)
        if ts:
            if created_at is None:
                created_at = ts
            updated_at = ts

        msg_id = entry.get("uuid", f"{session_id}:{len(messages)}")

        # metadata
        metadata: dict = {}
        if role == "assistant":
            model = msg_data.get("model")
            if model:
                metadata["model"] = model
            usage = msg_data.get("usage", {})
            if usage:
                metadata["usage"] = usage

        messages.append(IngestedMessage(
            id=f"claude_code:{msg_id}",
            thread_id=thread_id,
            role=role,
            content=content,
            created_at=ts,
            metadata=metadata,
        ))

    if not messages:
        return None

    # Try to derive title from first user message
    first_user = next((m for m in messages if m.role == "user"), None)
    title = None
    if first_user:
        title = first_user.content[:80].split("\n")[0].strip()

    return IngestedThread(
        id=thread_id,
        source_id="claude_code",
        title=title or session_id,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
    )


class ClaudeCodeIngestor(BaseIngestor):
    source_id = "claude_code"

    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = root

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        pass  # no setup needed

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        if not self.root.exists():
            return
        for jsonl_path in sorted(self.root.rglob("*.jsonl")):
            thread = _parse_jsonl(jsonl_path)
            if thread is None:
                continue
            if since and thread.updated_at and thread.updated_at < since:
                continue
            yield thread
