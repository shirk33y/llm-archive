from __future__ import annotations
import json
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.ingestors.web import parse_timestamp as _parse_timestamp
from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread, ToolCall

DEFAULT_ROOT = Path.home() / ".claude" / "projects"


def _flatten_content(content) -> str:
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
            text = _flatten_content(block.get("content", ""))[:500]
            parts.append(f"[Tool result]\n{text}")
        else:
            for key in ("text", "content", "output"):
                val = block.get(key)
                if val and isinstance(val, str):
                    parts.append(val)
                    break
    return "\n\n".join(p for p in parts if p.strip())


def _process_content_blocks(
    blocks, pending_tool_uses: dict[str, dict]
) -> tuple[str, list[IngestedPart]]:
    """Process content blocks into flat text + structured parts with ToolCall linking.

    pending_tool_uses tracks tool_use_ids across message entries for tool_use→result linking.
    """
    if isinstance(blocks, str):
        return blocks, [IngestedPart(kind="text", text=blocks)]
    if not isinstance(blocks, list):
        return str(blocks), [IngestedPart(kind="text", text=str(blocks))]

    parts: list[IngestedPart] = []
    text_chunks: list[str] = []

    for block in blocks:
        if not isinstance(block, dict):
            chunk = str(block)
            text_chunks.append(chunk)
            parts.append(IngestedPart(kind="text", text=chunk))
            continue

        btype = block.get("type", "")

        if btype == "text":
            text = block.get("text", "")
            if text and text.strip():
                text_chunks.append(text)
                parts.append(IngestedPart(kind="text", text=text))

        elif btype == "thinking":
            thinking = block.get("thinking", "")
            if thinking and thinking.strip():
                text_chunks.append(f"[Thinking]\n{thinking}")
                parts.append(
                    IngestedPart(kind="reasoning", text=thinking, visible=True, searchable=False)
                )

        elif btype == "tool_use":
            tool_id = block.get("id", "")
            name = block.get("name", "tool")
            inp = block.get("input", {})
            pending_tool_uses[tool_id] = {"name": name, "input": inp}

            cmd = inp.get("command", inp.get("code", json.dumps(inp, ensure_ascii=False)[:200]))
            text_chunks.append(f"[Tool: {name}]\n{cmd}")

            tc = ToolCall(tool_use_id=tool_id, name=name, input=inp)
            parts.append(IngestedPart(kind="tool_call", text=cmd, tool_call=tc))

        elif btype == "tool_result":
            tool_id = block.get("tool_use_id", "")
            content = block.get("content", "")
            is_error = block.get("is_error", False)

            if isinstance(content, list):
                result_text = _flatten_content(content)
            else:
                result_text = str(content) if content is not None else ""

            tc = ToolCall(tool_use_id=tool_id, result=result_text, is_error=bool(is_error))
            if tool_id in pending_tool_uses:
                tc.name = pending_tool_uses[tool_id]["name"]
                tc.input = pending_tool_uses[tool_id]["input"]

            text_chunks.append(f"[Tool result]\n{result_text[:500]}")
            parts.append(IngestedPart(kind="tool_result", text=result_text, tool_call=tc))

        else:
            for key in ("text", "content", "output"):
                val = block.get(key)
                if val and isinstance(val, str):
                    text_chunks.append(val)
                    parts.append(IngestedPart(kind="text", text=val))
                    break

    content = "\n\n".join(p for p in text_chunks if p.strip())
    return content, parts


def _load_sessions_index(project_dir: Path) -> dict[str, dict]:
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

    session_id = None
    for entry in lines:
        session_id = entry.get("sessionId")
        if session_id:
            break
    if not session_id:
        session_id = path.stem

    thread_id = f"claudecode:{session_id}"
    messages: list[IngestedMessage] = []
    created_at = None
    updated_at = None
    pending_tool_uses: dict[str, dict] = {}

    for entry in lines:
        etype = entry.get("type")
        if etype in ("queue-operation", "file-history-snapshot"):
            continue

        msg_data = entry.get("message")
        if not msg_data:
            continue

        role = msg_data.get("role")
        if role not in ("user", "assistant"):
            continue

        content, parts = _process_content_blocks(msg_data.get("content", ""), pending_tool_uses)
        if not content.strip():
            continue

        ts = _parse_timestamp(entry.get("timestamp"))
        if ts:
            if created_at is None:
                created_at = ts
            updated_at = ts

        msg_id = entry.get("uuid", f"{session_id}:{len(messages)}")
        metadata: dict = {}
        if role == "assistant":
            model = msg_data.get("model")
            if model:
                metadata["model"] = model
            usage = msg_data.get("usage", {})
            if usage:
                metadata["usage"] = usage

        messages.append(IngestedMessage(
            id=f"claudecode:{msg_id}",
            thread_id=thread_id,
            role=role,
            content=content,
            created_at=ts,
            metadata=metadata,
            parts=parts,
        ))

    if not messages:
        return None

    meta = index_meta or {}
    title = meta.get("summary") or None
    if not title:
        first_user = next((m for m in messages if m.role == "user"), None)
        if first_user:
            title = first_user.content[:80].split("\n")[0].strip()

    if meta.get("created") and created_at is None:
        created_at = _parse_timestamp(meta["created"])
    if meta.get("modified") and updated_at is None:
        updated_at = _parse_timestamp(meta["modified"])

    return IngestedThread(
        id=thread_id,
        source_id="claudecode",
        title=title or session_id,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
    )


class ClaudeCodeIngestor(BaseIngestor):
    source_id = "claudecode"

    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = root

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        pass

    async def count_threads(self, since: int | None = None) -> int:
        if not self.root.exists():
            return 0
        count = 0
        for project_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            index = _load_sessions_index(project_dir)
            for jsonl_path in sorted(project_dir.glob("*.jsonl")):
                thread = _parse_jsonl(jsonl_path, index.get(jsonl_path.stem, {}))
                if thread is None:
                    continue
                if since and thread.updated_at and thread.updated_at < since:
                    continue
                count += 1
        return count

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        if not self.root.exists():
            return
        for project_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            index = _load_sessions_index(project_dir)
            for jsonl_path in sorted(project_dir.glob("*.jsonl")):
                thread = _parse_jsonl(jsonl_path, index.get(jsonl_path.stem, {}))
                if thread is None:
                    continue
                if since and thread.updated_at and thread.updated_at < since:
                    continue
                yield thread
