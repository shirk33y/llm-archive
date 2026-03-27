from __future__ import annotations
import json
import re
from pathlib import Path
from typing import AsyncIterator

# Tags injected by Claude Code IDE integration that pollute message content
_STRIP_TAGS = re.compile(
    r'<(?:ide_opened_file|local-command-caveat|command-name|command-message|command-args|system-reminder)'
    r'[\s\S]*?</[^>]+>',
    re.DOTALL,
)


def _clean_user_text(s: str) -> str:
    """Remove Claude Code IDE injection tags, collapse whitespace."""
    s = _STRIP_TAGS.sub('', s)
    return re.sub(r'\s+', ' ', s).strip()

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedThread

DEFAULT_ROOT = Path.home() / ".claude" / "projects"


def _parse_timestamp(ts) -> int | None:
    """Parse ISO string, epoch seconds, or epoch milliseconds → epoch ms."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts) if ts > 1e12 else int(ts * 1000)
    if isinstance(ts, str):
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            try:
                v = float(ts)
                return int(v) if v > 1e12 else int(v * 1000)
            except Exception:
                return None
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
            text = _flatten_content(content_inner)[:500]
            parts.append(f"[Tool result]\n{text}")
        else:
            # unknown block type — try to extract any text-like field
            for key in ("text", "content", "output"):
                val = block.get(key)
                if val and isinstance(val, str):
                    parts.append(val)
                    break
    return "\n\n".join(p for p in parts if p.strip())


def _load_sessions_index(project_dir: Path) -> dict[str, dict]:
    """Load sessions-index.json and return a dict keyed by sessionId."""
    index_path = project_dir / "sessions-index.json"
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        return {e["sessionId"]: e for e in data.get("entries", []) if "sessionId" in e}
    except Exception:
        return {}


def _parse_jsonl(path: Path, index_meta: dict | None = None) -> IngestedThread | None:
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
        if role == "user":
            content = _clean_user_text(content)
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

    # Prefer summary from sessions-index.json; fall back to first user message
    meta = index_meta or {}
    title = meta.get("summary") or None
    if not title:
        first_user = next((m for m in messages if m.role == "user"), None)
        if first_user:
            title = first_user.content[:80].split("\n")[0].strip()

    # Use index timestamps if available (more reliable than JSONL)
    if meta.get("created") and created_at is None:
        created_at = _parse_timestamp(meta["created"])
    if meta.get("modified") and updated_at is None:
        updated_at = _parse_timestamp(meta["modified"])

    thread_metadata: dict = {}
    if meta.get("projectPath"):
        thread_metadata["projectPath"] = meta["projectPath"]

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
        for project_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            index = _load_sessions_index(project_dir)
            for jsonl_path in sorted(project_dir.glob("*.jsonl")):
                meta = index.get(jsonl_path.stem, {})
                thread = _parse_jsonl(jsonl_path, meta)
                if thread is None:
                    continue
                if since and thread.updated_at and thread.updated_at < since:
                    continue
                yield thread
