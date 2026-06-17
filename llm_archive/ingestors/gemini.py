from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.ingestors.claudecode import _parse_timestamp
from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread, ToolCall

DEFAULT_ROOT = Path.home() / ".gemini" / "tmp"

TOOL_NAME_MAP = {
    "run_shell_command": "Bash",
    "shell": "Bash",
    "read_file": "Read",
    "read_many_files": "Read",
    "write_file": "Write",
    "write_to_file": "Write",
    "edit_file": "Edit",
    "replace": "Edit",
    "list_directory": "Glob",
    "search_files": "Grep",
    "grep_search": "Grep",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
}


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
        text = block.get("text") or block.get("content")
        if isinstance(text, str):
            chunks.append(text)
    return "\n\n".join(chunk for chunk in chunks if chunk.strip())


def _tool_input(name: str, args: object) -> dict | None:
    if not isinstance(args, dict):
        return None
    mapped_name = TOOL_NAME_MAP.get(name, name)
    if mapped_name == "Bash" and isinstance(args.get("command"), str):
        return {"command": args["command"]}
    return dict(args)


def _tool_result(result: object) -> str | None:
    if isinstance(result, str):
        return result or None
    if not isinstance(result, list):
        return None
    chunks: list[str] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        response = ((item.get("functionResponse") or {}).get("response") or {})
        if not isinstance(response, dict):
            continue
        output = response.get("output")
        error = response.get("error")
        if isinstance(output, str) and output:
            chunks.append(output)
        elif isinstance(error, str) and error and error != "(none)":
            chunks.append(error)
    return "\n\n".join(chunks) or None


def _tool_is_error(tool_call: dict) -> bool:
    status = tool_call.get("status")
    if isinstance(status, str) and status.lower() == "error":
        return True
    result = tool_call.get("result")
    if not isinstance(result, list):
        return False
    for item in result:
        if not isinstance(item, dict):
            continue
        response = ((item.get("functionResponse") or {}).get("response") or {})
        if not isinstance(response, dict):
            continue
        exit_code = response.get("exitCode")
        if exit_code not in (None, 0):
            return True
    return False


def _thought_text(thought: dict) -> str:
    subject = thought.get("subject")
    description = thought.get("description")
    if isinstance(subject, str) and isinstance(description, str):
        return f"{subject}: {description}"
    if isinstance(description, str):
        return description
    if isinstance(subject, str):
        return subject
    return ""


def _assistant_parts(entries: list[dict]) -> tuple[str, list[IngestedPart], dict]:
    content_chunks: list[str] = []
    parts: list[IngestedPart] = []
    metadata: dict = {}

    for entry in entries:
        model = entry.get("model")
        if model:
            metadata["model"] = model
        tokens = entry.get("tokens")
        if tokens:
            metadata["tokens"] = tokens

        for thought in entry.get("thoughts") or []:
            if not isinstance(thought, dict):
                continue
            text = _thought_text(thought)
            if not text:
                continue
            content_chunks.append(f"[Reasoning]\n{text}")
            parts.append(IngestedPart(kind="reasoning", text=text, visible=True, searchable=False))

        for raw_call in entry.get("toolCalls") or []:
            if not isinstance(raw_call, dict):
                continue
            name = str(raw_call.get("name") or "tool")
            mapped_name = TOOL_NAME_MAP.get(name, name)
            inp = _tool_input(name, raw_call.get("args"))
            result = _tool_result(raw_call.get("result"))
            tc = ToolCall(
                tool_use_id=str(raw_call.get("id") or ""),
                name=mapped_name,
                input=inp,
                result=result,
                resultTimestamp=_parse_timestamp(raw_call.get("timestamp")),
                is_error=_tool_is_error(raw_call),
            )
            preview = ""
            if inp:
                preview = str(inp.get("command") or inp.get("file_path") or json.dumps(inp))[:200]
            content_chunks.append(f"[Tool: {mapped_name}]\n{preview}")
            if result:
                content_chunks.append(f"[Tool result]\n{result}")
            parts.append(IngestedPart(kind="tool_call", text=preview, tool_call=tc))
            if result:
                parts.append(IngestedPart(kind="tool_result", text=result, tool_call=tc))

        content = _flatten_content(entry.get("content", ""))
        if content.strip():
            content_chunks.append(content)
            parts.append(IngestedPart(kind="text", text=content))

    return "\n\n".join(chunk for chunk in content_chunks if chunk.strip()), parts, metadata


def _parse_json(path: Path) -> IngestedThread | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        return None

    session_id = str(data.get("sessionId") or path.stem)
    thread_id = f"gemini:{session_id}"
    messages: list[IngestedMessage] = []
    assistant_entries: list[dict] = []

    def flush_assistant() -> None:
        if not assistant_entries:
            return
        content, parts, metadata = _assistant_parts(assistant_entries)
        if not content.strip():
            assistant_entries.clear()
            return
        first = assistant_entries[0]
        msg_id = first.get("id") or f"{session_id}:assistant:{len(messages)}"
        messages.append(
            IngestedMessage(
                id=f"gemini:{msg_id}",
                thread_id=thread_id,
                role="assistant",
                content=content,
                created_at=_parse_timestamp(first.get("timestamp")),
                metadata=metadata,
                parts=parts,
                raw={"entries": assistant_entries.copy()},
            )
        )
        assistant_entries.clear()

    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        role = raw.get("type") or raw.get("role")
        if role == "gemini":
            assistant_entries.append(raw)
            continue
        if role != "user":
            continue

        flush_assistant()
        content = _flatten_content(raw.get("content", ""))
        if not content.strip():
            continue
        msg_id = raw.get("id") or f"{session_id}:user:{len(messages)}"
        messages.append(
            IngestedMessage(
                id=f"gemini:{msg_id}",
                thread_id=thread_id,
                role="user",
                content=content,
                created_at=_parse_timestamp(raw.get("timestamp")),
                parts=[IngestedPart(kind="text", text=content)],
                raw=raw,
            )
        )

    flush_assistant()

    if not messages:
        return None

    first_user = next((message for message in messages if message.role == "user"), None)
    title = first_user.content[:80].split("\n")[0].strip() if first_user else session_id
    return IngestedThread(
        id=thread_id,
        source_id="gemini",
        title=title or session_id,
        created_at=_parse_timestamp(data.get("startTime")),
        updated_at=_parse_timestamp(data.get("lastUpdated")),
        messages=messages,
    )


class GeminiIngestor(BaseIngestor):
    source_id = "gemini"

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
            thread = _parse_json(path)
            if thread:
                yield thread

    def _paths(self, since: int | None) -> list[Path]:
        if not self.root.exists():
            return []
        paths = sorted(self.root.glob("*/chats/*.json"))
        if since is None:
            return paths
        return [path for path in paths if int(path.stat().st_mtime * 1000) >= since]
