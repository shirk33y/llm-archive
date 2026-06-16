from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread, ToolCall

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

TOOL_NAME_MAP = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "ls": "Glob",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "codesearch": "Grep",
    "task": "Task",
    "todowrite": "TodoWrite",
    "prune": "Prune",
    "question": "Question",
    "distill": "Distill",
    "compress": "Compress",
    "skill": "Skill",
}


def _map_tool_name(name: str) -> str:
    return TOOL_NAME_MAP.get(name, name)


def _parse_tool_part(data: dict) -> IngestedPart | None:
    state = data.get("state", {})
    input_data = state.get("input", {})
    output = state.get("output", "")
    if output is None:
        output = ""
    tool_name = data.get("tool", "unknown")
    mapped_name = _map_tool_name(tool_name)

    if mapped_name == "Bash" and isinstance(input_data, dict) and "command" in input_data:
        workdir = input_data.get("workdir")
        command = input_data.get("command", "")
        if workdir:
            input_data = {"command": f"cd {workdir} && {command}"}
        else:
            input_data = {"command": command}

    status = state.get("status")
    if isinstance(status, str):
        status = status.lower()
    is_error = status == "error"
    if not is_error:
        exit_code = (state.get("metadata") or {}).get("exit")
        if exit_code is not None and exit_code != 0:
            is_error = True

    tc = ToolCall(
        tool_use_id=data.get("callID", ""),
        name=mapped_name,
        input=input_data if isinstance(input_data, dict) else None,
        result=str(output) if output else None,
        is_error=is_error,
    )

    text = str(input_data.get("command", json.dumps(input_data, ensure_ascii=False) if input_data else ""))[:200]
    return IngestedPart(kind="tool_call", text=text, tool_call=tc)


class OpenCodeIngestor(BaseIngestor):
    source_id = "opencode"

    def __init__(self, db_path: Path = DEFAULT_DB):
        self.db_path = db_path

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        pass

    async def count_threads(self, since: int | None = None) -> int:
        if not self.db_path.exists():
            return 0
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            query = "SELECT COUNT(*) FROM session"
            params: list = []
            if since:
                query += " WHERE time_updated >= ?"
                params.append(since)
            row = con.execute(query, params).fetchone()
            return row[0] if row else 0
        finally:
            con.close()

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        if not self.db_path.exists():
            return

        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        try:
            query = "SELECT id, title, time_created, time_updated FROM session"
            params: list = []
            if since:
                query += " WHERE time_updated >= ?"
                params.append(since)
            query += " ORDER BY time_created"

            sessions = con.execute(query, params).fetchall()
            for sess in sessions:
                thread = _build_thread(con, sess)
                if thread:
                    yield thread
        finally:
            con.close()


def _build_thread(con: sqlite3.Connection, sess) -> IngestedThread | None:
    sess_id = sess["id"]
    thread_id = f"opencode:{sess_id}"

    # messages for this session, ordered by creation time
    rows = con.execute(
        "SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created",
        (sess_id,),
    ).fetchall()

    if not rows:
        return None

    messages: list[IngestedMessage] = []

    for row in rows:
        msg_data = json.loads(row["data"])
        role = msg_data.get("role")
        if role not in ("user", "assistant"):
            continue

        # Fetch parts for this message to get text content
        part_rows = con.execute(
            "SELECT data FROM part WHERE message_id=? ORDER BY time_created",
            (row["id"],),
        ).fetchall()

        content_parts: list[str] = []
        ingested_parts: list[IngestedPart] = []

        for part_row in part_rows:
            part = json.loads(part_row["data"])
            ptype = part.get("type", "")

            if ptype == "text":
                text = part.get("text", "")
                if text and text.strip():
                    content_parts.append(text)
                    ingested_parts.append(IngestedPart(kind="text", text=text))

            elif ptype == "reasoning":
                text = part.get("reasoning", part.get("text", ""))
                if text and text.strip():
                    content_parts.append(f"[Reasoning]\n{text}")
                    ingested_parts.append(IngestedPart(kind="reasoning", text=text, visible=True, searchable=False))

            elif ptype == "tool":
                tp = _parse_tool_part(part)
                if tp and tp.tool_call:
                    tc = tp.tool_call
                    cmd_preview = (tc.input or {}).get("command", str(tc.input if tc.input else ""))[:200]
                    content_parts.append(f"[Tool: {tc.name}]\n{cmd_preview}")
                    if tc.result:
                        content_parts.append(f"[Tool result]\n{tc.result}")
                    ingested_parts.append(tp)
                    # Also add a tool_result part with the result
                    if tc.result:
                        ingested_parts.append(IngestedPart(kind="tool_result", text=tc.result, tool_call=tc))

            elif ptype == "patch":
                text = part.get("patch", part.get("text", ""))
                if text and text.strip():
                    content_parts.append(f"[Patch]\n{text}")

            elif ptype == "file":
                path = part.get("path", part.get("text", ""))
                if path:
                    content_parts.append(f"[File: {path}]")

            # skip step-start/step-finish/compaction/subtask

        content = "\n\n".join(p for p in content_parts if p.strip())
        if not content.strip():
            continue

        ts = row["time_created"]
        metadata: dict = {}
        model_info = msg_data.get("model", {})
        if isinstance(model_info, dict):
            provider = model_info.get("providerID")
            model_id = model_info.get("modelID")
            if provider or model_id:
                metadata["model"] = f"{provider}/{model_id}" if provider and model_id else (provider or model_id)

        messages.append(IngestedMessage(
            id=f"opencode:{row['id']}",
            thread_id=thread_id,
            role=role,
            content=content,
            created_at=ts,
            metadata=metadata,
            parts=ingested_parts,
        ))

    if not messages:
        return None

    return IngestedThread(
        id=thread_id,
        source_id="opencode",
        title=sess["title"],
        created_at=sess["time_created"],
        updated_at=sess["time_updated"],
        messages=messages,
    )
