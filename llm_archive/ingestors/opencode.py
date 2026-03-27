from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedThread

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


class OpenCodeIngestor(BaseIngestor):
    source_id = "opencode"

    def __init__(self, db_path: Path = DEFAULT_DB):
        self.db_path = db_path

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        pass

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
        parts = con.execute(
            "SELECT data FROM part WHERE message_id=? ORDER BY time_created",
            (row["id"],),
        ).fetchall()

        content_parts = []
        for part_row in parts:
            part = json.loads(part_row["data"])
            ptype = part.get("type", "")
            if ptype == "text":
                text = part.get("text", "")
                if text:
                    content_parts.append(text)
            elif ptype == "reasoning":
                text = part.get("reasoning", part.get("text", ""))
                if text:
                    content_parts.append(f"[Reasoning]\n{text}")
            elif ptype == "tool-invocation":
                tool = part.get("toolInvocation", {})
                name = tool.get("toolName", "tool")
                args = tool.get("args", {})
                result = tool.get("result", "")
                content_parts.append(f"[Tool: {name}] {json.dumps(args)[:200]}")
                if result:
                    content_parts.append(f"[Tool result] {str(result)[:500]}")
            # skip step-start/step-finish markers

        content = "\n\n".join(p for p in content_parts if p.strip())
        if not content.strip():
            continue

        ts = row["time_created"]  # already milliseconds

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
