from __future__ import annotations
import json
import re
import sqlite3
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread, ToolCall

STATE_DB = Path.home() / ".codex" / "state_5.sqlite"
SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

ROLLOUT_RE = re.compile(r"rollout-.*?-([a-f0-9-]+)\.jsonl$")

TOOL_NAME_MAP = {
    "exec_command": "Bash",
    "apply_patch": "Edit",
    "read_file": "Read",
    "write_file": "Write",
    "list_directory": "Glob",
    "search_files": "Grep",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "run_terminal_command": "Bash",
}

ERROR_PATTERNS = re.compile(r"Error:|exit code|failed|FAILED|command not found|no such file|permission denied", re.IGNORECASE)


def _normalize_codex_command(cmd: str) -> str:
    cmd = re.sub(r"^/bin/bash\s+-lc\s+", "", cmd)
    cmd = cmd.strip().strip("'").strip('"')
    return cmd


def _map_codex_tool_name(name: str) -> str:
    return TOOL_NAME_MAP.get(name, name)


def _detect_error(output: str) -> bool:
    return bool(isinstance(output, str) and ERROR_PATTERNS.search(output))


def _parse_codex_function_call(name: str, args: str, call_id: str) -> ToolCall | None:
    mapped_name = _map_codex_tool_name(name)
    input_data: dict | None = None
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError:
        parsed = None

    if name == "exec_command":
        mapped_name = "Bash"
        cmd = (parsed or {}).get("cmd", args)
        cmd = _normalize_codex_command(cmd)
        input_data = {"command": cmd}
        workdir = (parsed or {}).get("workdir")
        if workdir:
            input_data["workdir"] = workdir
    elif name == "apply_patch":
        if parsed:
            mapped_name = "Edit" if "file_path" in parsed else "Write"
            input_data = parsed
        else:
            input_data = _parse_codex_patch(args)
    elif parsed:
        input_data = parsed

    return ToolCall(
        tool_use_id=call_id,
        name=mapped_name,
        input=input_data,
    )


def _parse_codex_patch(patch_str: str) -> dict:
    lines = patch_str.split("\n")
    file_path = ""
    old_lines = []
    new_lines = []
    is_new = False
    for line in lines:
        if line.startswith("*** Add File:"):
            file_path = line.replace("*** Add File:", "").strip()
            is_new = True
        elif line.startswith("*** Update File:"):
            file_path = line.replace("*** Update File:", "").strip()
            is_new = False
        elif line.startswith("@@") or line.startswith("***"):
            continue
        elif line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith("-"):
            old_lines.append(line[1:])
        else:
            old_lines.append(line)
            new_lines.append(line)
    if is_new:
        return {"file_path": file_path, "content": "\n".join(new_lines)}
    return {
        "file_path": file_path,
        "old_string": "\n".join(old_lines),
        "new_string": "\n".join(new_lines),
    }


class CodexIngestor(BaseIngestor):
    source_id = "codex"

    def __init__(self, state_db: Path = STATE_DB, sessions_root: Path = SESSIONS_ROOT):
        self.state_db = state_db
        self.sessions_root = sessions_root

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        pass

    async def count_threads(self, since: int | None = None) -> int:
        if not self.state_db.exists():
            return 0
        con = sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True)
        try:
            query = "SELECT COUNT(*) FROM threads"
            params: list = []
            if since:
                query += " WHERE updated_at >= ?"
                params.append(since)
            row = con.execute(query, params).fetchone()
            return row[0] if row else 0
        finally:
            con.close()

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        if not self.state_db.exists():
            return

        thread_map = _load_thread_map(self.state_db, since)
        for thread_id, meta in thread_map.items():
            path = _find_session_file(self.sessions_root, thread_id)
            if not path:
                continue
            thread = _parse_session(path, thread_id, meta)
            if thread:
                yield thread


def _load_thread_map(state_db: Path, since: int | None = None) -> dict[str, dict]:
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    result: dict[str, dict] = {}
    try:
        query = "SELECT id, title, created_at, updated_at, model, model_provider, preview FROM threads"
        params: list = []
        if since:
            query += " WHERE updated_at >= ?"
            params.append(since)
        for row in con.execute(query, params).fetchall():
            result[row["id"]] = dict(row)
    finally:
        con.close()
    return result


def _find_session_file(sessions_root: Path, thread_id: str) -> Path | None:
    rollout_pattern = f"rollout-*-{thread_id}.jsonl"
    for path in sessions_root.rglob(rollout_pattern):
        return path
    return None


def _parse_ts(ts: str) -> int | None:
    if not ts:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _extract_text(content) -> str:
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
        if btype in ("input_text", "output_text", "text"):
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n\n".join(p for p in parts if p.strip())


def _parse_session(path: Path, thread_id: str, meta: dict) -> IngestedThread | None:
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

    tid = f"codex:{thread_id}"
    messages: list[IngestedMessage] = []
    first_ts: int | None = None
    last_ts: int | None = None
    pending_tool_calls: dict[str, ToolCall] = {}

    for entry in lines:
        etype = entry.get("type")
        if etype == "session_meta":
            continue
        if etype == "turn_context":
            continue
        if etype == "event_msg":
            continue

        if etype != "response_item":
            continue

        payload = entry.get("payload", {})
        ts = _parse_ts(entry.get("timestamp", ""))
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        ptype = payload.get("type")

        if ptype == "message":
            role = payload.get("role")
            if role == "developer":
                continue
            if role not in ("user", "assistant"):
                continue

            content = _extract_text(payload.get("content", ""))
            if not content.strip():
                continue

            msg_id = entry.get("uuid") or payload.get("id") or f"{thread_id}:{len(messages)}"
            metadata: dict = {}
            if role == "assistant":
                model_id = meta.get("model")
                if model_id:
                    metadata["model"] = model_id

            messages.append(IngestedMessage(
                id=f"codex:{msg_id}",
                thread_id=tid,
                role=role,
                content=content,
                created_at=ts,
                metadata=metadata,
            ))

        elif ptype == "function_call":
            name = payload.get("name", "tool")
            args = payload.get("arguments", "")
            call_id = payload.get("call_id", "")
            tc = _parse_codex_function_call(name, args, call_id)
            if tc:
                pending_tool_calls[call_id] = tc
                cmd_preview = (tc.input or {}).get("command", args)[:200]
            else:
                cmd_preview = args[:200]
            content = f"[Tool: {tc.name if tc else name}]\n{cmd_preview}"
            parts = []
            if tc:
                parts.append(IngestedPart(kind="tool_call", text=cmd_preview, tool_call=tc))
            msg_id = call_id or f"{thread_id}:fc:{len(messages)}"
            messages.append(IngestedMessage(
                id=f"codex:{msg_id}",
                thread_id=tid,
                role="assistant",
                content=content,
                created_at=ts,
                metadata={},
                parts=parts,
            ))

        elif ptype == "function_call_output":
            call_id = payload.get("call_id", "")
            output = payload.get("output")
            if output is None:
                output = ""
            elif not isinstance(output, str):
                output = str(output)
            is_error = _detect_error(output)
            tc = pending_tool_calls.get(call_id)
            if tc:
                tc.result = output
                if is_error:
                    tc.is_error = True
            content = f"[Tool result]\n{output}"
            parts = []
            if tc:
                parts.append(IngestedPart(kind="tool_result", text=output, tool_call=tc))
            messages.append(IngestedMessage(
                id=f"codex:{call_id}:result" if call_id else f"codex:{thread_id}:fcr:{len(messages)}",
                thread_id=tid,
                role="tool",
                content=content,
                created_at=ts,
                metadata={},
                parts=parts,
            ))

        elif ptype == "reasoning":
            continue

    if not messages:
        return None

    title = meta.get("title") or None
    if not title:
        first_user = next((m for m in messages if m.role == "user"), None)
        if first_user:
            title = first_user.content[:80].split("\n")[0].strip()

    created = meta.get("created_at") or first_ts
    updated = meta.get("updated_at") or last_ts

    return IngestedThread(
        id=tid,
        source_id="codex",
        title=title or thread_id,
        created_at=created,
        updated_at=updated,
        messages=messages,
    )
