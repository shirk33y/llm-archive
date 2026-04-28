from __future__ import annotations
import hashlib
import json
import re
import socket
import sqlite3
import time
from pathlib import Path

from llm_archive.logging import get_logger
from llm_archive.schema import IngestedThread, IngestedMessage, IngestedPart

logger = get_logger("db")

DB_PATH = Path.home() / ".llm-archive" / "archive.db"

BASE53 = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"


def to_base53(num: int) -> str:
    """Encode integer to base53 string."""
    if num == 0:
        return BASE53[0]
    digits = []
    while num:
        digits.append(BASE53[num % 53])
        num //= 53
    return "".join(reversed(digits))


def from_base53(s: str) -> int:
    """Decode base53 string to integer."""
    result = 0
    for char in s:
        result = result * 53 + BASE53.index(char)
    return result


# Tags injected by Claude Code IDE/system that pollute user message content.
# Extend this list as new injection patterns are discovered.
_INJECTION_TAGS = re.compile(
    r"<(?:"
    r"ide_opened_file"
    r"|local-command-caveat"
    r"|command-name"
    r"|command-message"
    r"|command-args"
    r"|system-reminder"
    r"|user-prompt-submit-hook"
    r")[\s\S]*?</[^>]+>",
    re.DOTALL,
)


def clean_content(text: str) -> str:
    """Strip known IDE/system injection tags from message content.

    Raw content is stored in the database; call this at read/display time.
    To add a new tag: extend _INJECTION_TAGS above — applies to all future reads.
    """
    if not text:
        return text
    cleaned = _INJECTION_TAGS.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _migrate_windsurf_prefix(con: sqlite3.Connection) -> None:
    """Migrate windsurf:ls: thread IDs to windsurf: (one-time migration)."""
    # Check if any threads still have the old prefix
    old_threads = con.execute(
        "SELECT id FROM threads WHERE id LIKE 'windsurf:ls:%'"
    ).fetchall()
    if not old_threads:
        return

    logger.info(f"Migrating {len(old_threads)} windsurf thread IDs from 'windsurf:ls:' to 'windsurf:'")

    # Disable foreign key checks for the migration
    con.execute("PRAGMA foreign_keys=OFF")

    try:
        # For each old thread ID, create new ID and migrate data
        for (old_id,) in old_threads:
            new_id = old_id.replace("windsurf:ls:", "windsurf:", 1)

            # Get the thread data
            thread = con.execute(
                "SELECT source_id, title, created_at, updated_at, sha1, content_checked_at FROM threads WHERE id=?",
                (old_id,)
            ).fetchone()

            if thread:
                # Insert new thread with updated ID (FK checks disabled)
                con.execute(
                    "INSERT INTO threads(id, source_id, title, created_at, updated_at, sha1, content_checked_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (new_id, thread["source_id"], thread["title"], thread["created_at"],
                     thread["updated_at"], thread["sha1"], thread["content_checked_at"])
                )

            # Now update messages to point to new thread ID
            con.execute(
                "UPDATE messages SET thread_id=? WHERE thread_id=?",
                (new_id, old_id)
            )

            # Update messages_fts to point to new thread ID
            con.execute(
                "UPDATE messages_fts SET thread_id=? WHERE thread_id=?",
                (new_id, old_id)
            )

            # Delete old thread (now safe since new thread exists and messages updated)
            con.execute("DELETE FROM threads WHERE id=?", (old_id,))
    finally:
        # Re-enable foreign key checks
        con.execute("PRAGMA foreign_keys=ON")


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    last_sync   INTEGER,
    config      TEXT,
    hostname    TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    id                  TEXT PRIMARY KEY,
    source_id           TEXT    NOT NULL,
    title               TEXT,
    created_at          INTEGER,
    updated_at          INTEGER,
    sha1                TEXT,
    content_checked_at  INTEGER,
    FOREIGN KEY (source_id) REFERENCES sources(id)
);
CREATE INDEX IF NOT EXISTS idx_threads_checked ON threads(source_id, content_checked_at);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    content_clean   TEXT,
    created_at      INTEGER,
    metadata        TEXT,
    FOREIGN KEY (thread_id) REFERENCES threads(id)
);

CREATE TABLE IF NOT EXISTS message_parts (
    message_id       TEXT    NOT NULL,
    ord              INTEGER NOT NULL,
    kind             TEXT    NOT NULL,
    text             TEXT    NOT NULL,
    search_text      TEXT,
    data             TEXT,
    visible          INTEGER NOT NULL,
    searchable       INTEGER NOT NULL,
    PRIMARY KEY (message_id, ord),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS message_raw (
    message_id       TEXT PRIMARY KEY,
    raw              TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread  ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_threads_source   ON threads(source_id);
CREATE INDEX IF NOT EXISTS idx_threads_updated  ON threads(source_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_threads_updated_at ON threads(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_created  ON messages(thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_parts_message ON message_parts(message_id, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    id UNINDEXED,
    thread_id UNINDEXED,
    content_clean,
    prefix='2 3 4 5'
);
CREATE VIRTUAL TABLE IF NOT EXISTS message_parts_fts USING fts5(
    message_id UNINDEXED,
    ord UNINDEXED,
    search_text,
    prefix='2 3 4 5'
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    for stmt in DDL.split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)
    if not con.execute("SELECT count(*) FROM messages_fts").fetchone()[0]:
        con.execute(
            "INSERT INTO messages_fts(id, thread_id, content_clean) "
            "SELECT id, thread_id, content_clean FROM messages"
        )
    if (
        not con.execute("SELECT count(*) FROM message_parts_fts").fetchone()[0]
        and con.execute("SELECT count(*) FROM message_parts").fetchone()[0]
    ):
        con.execute(
            "INSERT INTO message_parts_fts(message_id, ord, search_text) "
            "SELECT message_id, ord, search_text FROM message_parts WHERE searchable=1"
        )
    _migrate_windsurf_prefix(con)
    con.commit()
    return con


def _thread_sha1(thread: IngestedThread) -> str:
    msgs_sorted = sorted(thread.messages, key=lambda m: (m.created_at or 0, m.id))
    payload = thread.id + "".join(m.content for m in msgs_sorted)
    return hashlib.sha1(payload.encode()).hexdigest()


def upsert_source(con: sqlite3.Connection, source_id: str, config: dict) -> None:
    hostname = socket.gethostname()
    con.execute(
        "INSERT INTO sources(id, config, hostname) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET config=excluded.config, hostname=excluded.hostname",
        (source_id, json.dumps(config), hostname),
    )
    con.commit()


def set_last_sync(con: sqlite3.Connection, source_id: str, ts: int) -> None:
    con.execute(
        "INSERT INTO sources(id, last_sync) VALUES(?,?) "
        "ON CONFLICT(id) DO UPDATE SET last_sync=excluded.last_sync",
        (source_id, ts),
    )
    con.commit()


def check_thread_sha1(con: sqlite3.Connection, thread: IngestedThread) -> bool:
    """Return True if thread's sha1 matches DB (no write)."""
    sha1 = _thread_sha1(thread)
    existing = con.execute("SELECT sha1 FROM threads WHERE id=?", (thread.id,)).fetchone()
    return existing is not None and existing["sha1"] == sha1


def bulk_update_timestamps(con: sqlite3.Connection, updates: dict[str, int]) -> None:
    """Update updated_at for threads where the new value is newer than stored."""
    if not updates:
        return
    con.executemany(
        "UPDATE threads SET updated_at=? WHERE id=? AND (updated_at IS NULL OR updated_at < ?)",
        [(ts, tid, ts) for tid, ts in updates.items()],
    )
    con.commit()


def get_last_sync(con: sqlite3.Connection, source_id: str) -> int | None:
    row = con.execute("SELECT last_sync FROM sources WHERE id=?", (source_id,)).fetchone()
    return row["last_sync"] if row else None


def save_thread(con: sqlite3.Connection, thread: IngestedThread, force: bool = False) -> bool:
    """Save thread + messages. Returns True if written, False if skipped (dedup)."""
    sha1 = _thread_sha1(thread)
    existing = con.execute("SELECT sha1 FROM threads WHERE id=?", (thread.id,)).fetchone()
    if existing:
        if existing["sha1"] == sha1:
            logger.info(f"sha1 match {thread.id} — skipping")
        else:
            logger.info(f"sha1 changed {thread.id} — old={existing['sha1']} new={sha1}")
    now_ms = int(time.time() * 1000)
    if not force and existing and existing["sha1"] == sha1:
        # Content unchanged — bump updated_at if API has a newer timestamp,
        # and always update content_checked_at so skip logic knows we verified recently.
        updates = ["content_checked_at=?"]
        params: list = [now_ms]
        if thread.updated_at is not None:
            updates.append("updated_at=CASE WHEN updated_at IS NULL OR updated_at < ? THEN ? ELSE updated_at END")
            params.extend([thread.updated_at, thread.updated_at])
        params.append(thread.id)
        con.execute(f"UPDATE threads SET {', '.join(updates)} WHERE id=?", params)
        con.commit()
        return False

    # Ensure source row exists (FK constraint)
    con.execute("INSERT OR IGNORE INTO sources(id) VALUES(?)", (thread.source_id,))

    con.execute(
        "INSERT OR REPLACE INTO threads(id, source_id, title, created_at, updated_at, sha1, content_checked_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (thread.id, thread.source_id, thread.title, thread.created_at, thread.updated_at, sha1, now_ms),
    )
    ids = [
        row[0]
        for row in con.execute("SELECT id FROM messages WHERE thread_id=?", (thread.id,)).fetchall()
    ]
    if ids:
        marks = ",".join("?" for _ in ids)
        con.execute(f"DELETE FROM message_parts_fts WHERE message_id IN ({marks})", ids)
        con.execute(f"DELETE FROM message_parts WHERE message_id IN ({marks})", ids)
        con.execute(f"DELETE FROM message_raw WHERE message_id IN ({marks})", ids)
    con.execute("DELETE FROM messages WHERE thread_id=?", (thread.id,))
    con.execute("DELETE FROM messages_fts WHERE thread_id=?", (thread.id,))
    for msg in thread.messages:
        clean = clean_content(msg.content)
        con.execute(
            "INSERT OR REPLACE INTO messages(id, thread_id, role, content, content_clean, created_at, metadata) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                msg.id,
                msg.thread_id,
                msg.role,
                msg.content,
                clean,
                msg.created_at,
                json.dumps(msg.metadata) if msg.metadata else None,
            ),
        )
        con.execute(
            "INSERT INTO messages_fts(id, thread_id, content_clean) VALUES(?,?,?)",
            (msg.id, msg.thread_id, clean),
        )
        for i, part in enumerate(_message_parts(msg)):
            text = _strip_content(part.text).strip()
            search_text = clean_content(text) if part.searchable else ""
            # Also add data field content to search_text for better searchability
            if part.data and part.searchable:
                data_str = json.dumps(part.data, ensure_ascii=False)
                search_text += " " + clean_content(data_str)
            con.execute(
                "INSERT OR REPLACE INTO message_parts(message_id, ord, kind, text, search_text, data, visible, searchable) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    msg.id,
                    i,
                    part.kind,
                    text,
                    search_text,
                    json.dumps(part.data) if part.data else None,
                    1 if part.visible else 0,
                    1 if part.searchable else 0,
                ),
            )
            if part.searchable and search_text:
                con.execute(
                    "INSERT INTO message_parts_fts(message_id, ord, search_text) VALUES(?,?,?)",
                    (msg.id, i, search_text),
                )
        if msg.raw is not None:
            con.execute(
                "INSERT OR REPLACE INTO message_raw(message_id, raw) VALUES(?,?)",
                (msg.id, json.dumps(msg.raw)),
            )
    con.commit()
    return True


def _message_parts(msg: IngestedMessage) -> list[IngestedPart]:
    return msg.parts if msg.parts else _parse_parts(msg.content)


def _parse_parts(text: str) -> list[IngestedPart]:
    body = _strip_content(text)
    if not body:
        return []
    parts = [_parse_part(block) for block in re.split(r"\n\s*\n+", body) if block.strip()]
    return [part for part in parts if part.text or part.data]


def _parse_part(text: str) -> IngestedPart:
    match = re.match(r"^\s*\[([^\]\n]{1,120})\]\s*(.*)$", text, re.DOTALL)
    if not match:
        return IngestedPart(kind="text", text=text)
    tag = match.group(1)
    body = match.group(2).strip()
    kind = _part_kind(tag)
    visible, searchable = _part_flags(kind)
    data = {"tag": tag}
    if tag.startswith("Tool: "):
        data["name"] = tag.removeprefix("Tool: ")
    return IngestedPart(kind=kind, text=body, data=data, visible=visible, searchable=searchable)


def _part_kind(tag: str) -> str:
    if tag.startswith("Tool: "):
        return "tool_call"
    if tag == "Tool result":
        return "tool_result"
    if tag in {"Thinking", "Reasoning"}:
        return "reasoning"
    if tag == "Search":
        return "search_query"
    if tag == "Search results":
        return "search_result"
    if tag.startswith("citation:"):
        return "citation"
    if tag.startswith("Request interrupted"):
        return "status"
    if tag.endswith("-mode") or tag.startswith("SYSTEM DIRECTIVE:"):
        return "directive"
    return "unknown"


def _part_flags(kind: str) -> tuple[bool, bool]:
    if kind in {"citation", "status"}:
        return True, False
    if kind == "directive":
        return True, False
    return True, True


def _strip_content(text: str) -> str:
    if not text:
        return text
    return _INJECTION_TAGS.sub("", text).strip()


def source_stats(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("""
        SELECT
            s.id,
            s.last_sync,
            s.config,
            s.hostname,
            COUNT(DISTINCT t.id) AS thread_count,
            COUNT(m.id) AS message_count
        FROM sources s
        LEFT JOIN threads t ON t.source_id = s.id
        LEFT JOIN messages m ON m.thread_id = t.id
        GROUP BY s.id
    """).fetchall()
    return [dict(r) for r in rows]


def search_messages(con: sqlite3.Connection, phrase: str, limit: int = 50) -> list[dict]:
    # First find matching message IDs (limit applies to distinct messages)
    # Then join parts for content — avoids LIMIT cutting off mid-message
    rows = con.execute(
        """
        SELECT
            t.source_id,
            t.id AS thread_id,
            t.title,
            m.id AS message_id,
            m.role,
            m.created_at,
            p.kind,
            p.text AS content_clean,
            p.ord AS ord,
            t.rowid AS thread_rowid,
            m.rowid AS message_rowid
        FROM (
            SELECT DISTINCT m.id
            FROM message_parts_fts
            JOIN messages m ON m.id = message_parts_fts.message_id
            JOIN threads t ON t.id = m.thread_id
            WHERE message_parts_fts MATCH ?
            ORDER BY t.updated_at DESC, m.created_at DESC
            LIMIT ?
        ) matched
        JOIN messages m ON m.id = matched.id
        JOIN threads t ON t.id = m.thread_id
        JOIN message_parts p ON p.message_id = m.id
        ORDER BY t.updated_at DESC, m.created_at DESC, m.id, p.ord
        """,
        (_fts_query(phrase), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def search_threads(con: sqlite3.Connection, phrase: str, limit: int = 50) -> list[dict]:
    rows = con.execute(
        """
        SELECT
            t.source_id,
            t.id AS thread_id,
            t.title,
            COUNT(*) AS match_count,
            MAX(m.created_at) AS last_match_at,
            t.rowid AS thread_rowid
        FROM message_parts_fts f
        JOIN message_parts p ON p.message_id = f.message_id AND p.ord = f.ord
        JOIN messages m ON m.id = p.message_id
        JOIN threads t ON t.id = m.thread_id
        WHERE message_parts_fts MATCH ?
        GROUP BY t.id, t.source_id, t.title
        ORDER BY last_match_at DESC
        LIMIT ?
        """,
        (_fts_query(phrase), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def list_threads(con: sqlite3.Connection, limit: int = 1000) -> list[dict]:
    """Return all threads sorted by newest first."""
    rows = con.execute(
        """
        SELECT
            source_id,
            id AS thread_id,
            title,
            updated_at,
            rowid AS thread_rowid
        FROM threads
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_thread(con: sqlite3.Connection, thread_id: str) -> dict | None:
    thread = con.execute(
        "SELECT id, source_id, title, created_at, updated_at FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    if not thread:
        return None
    return _fetch_thread_data(con, dict(thread))


def get_message(con: sqlite3.Connection, message_id: str) -> dict | None:
    """Fetch a single message with its parent thread info and parts."""
    msg = con.execute(
        "SELECT id, thread_id, role, created_at FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    if not msg:
        return None
    thread = con.execute(
        "SELECT id, source_id, title, created_at, updated_at FROM threads WHERE id=?",
        (msg["thread_id"],),
    ).fetchone()
    if not thread:
        return None
    messages = [_attach_parts(con, dict(msg))]
    return {"thread": dict(thread), "messages": messages}


def resolve_short_id(con: sqlite3.Connection, short_id: str) -> dict | None:
    """Resolve a short ID like 't5' or 'm42' to thread+messages data."""
    if short_id.startswith("t") and len(short_id) > 1:
        try:
            rowid = from_base53(short_id[1:])
        except (ValueError, IndexError):
            return None
        row = con.execute(
            "SELECT id, source_id, title, created_at, updated_at FROM threads WHERE rowid=?",
            (rowid,),
        ).fetchone()
        return _fetch_thread_data(con, dict(row)) if row else None
    if short_id.startswith("m") and len(short_id) > 1:
        try:
            rowid = from_base53(short_id[1:])
        except (ValueError, IndexError):
            return None
        msg = con.execute(
            "SELECT id, thread_id, role, created_at FROM messages WHERE rowid=?", (rowid,)
        ).fetchone()
        if not msg:
            return None
        thread = con.execute(
            "SELECT id, source_id, title, created_at, updated_at FROM threads WHERE id=?",
            (msg["thread_id"],),
        ).fetchone()
        if not thread:
            return None
        messages = [_attach_parts(con, dict(msg))]
        return {"thread": dict(thread), "messages": messages}
    return None


def _attach_parts(con: sqlite3.Connection, msg: dict) -> dict:
    """Attach parts to a message dict."""
    parts = [
        dict(r)
        for r in con.execute(
            "SELECT message_id, ord, kind, text, data, visible, searchable FROM message_parts "
            "WHERE message_id=? ORDER BY ord",
            (msg["id"],),
        ).fetchall()
    ]
    msg["parts"] = parts
    return msg


def _fetch_thread_data(con: sqlite3.Connection, thread: dict) -> dict:
    """Fetch messages with parts for a thread dict."""
    messages = [
        dict(row)
        for row in con.execute(
            "SELECT id, role, created_at FROM messages WHERE thread_id=? ORDER BY created_at, id",
            (thread["id"],),
        ).fetchall()
    ]
    parts = {}
    rows = con.execute(
        "SELECT message_parts.message_id, ord, kind, text, data, visible, searchable FROM message_parts "
        "JOIN messages ON messages.id = message_parts.message_id "
        "WHERE messages.thread_id=? ORDER BY messages.created_at, messages.id, ord",
        (thread["id"],),
    ).fetchall()
    for row in rows:
        parts.setdefault(row["message_id"], []).append(dict(row))
    for msg in messages:
        msg["parts"] = parts.get(msg["id"], [])
    return {"thread": thread, "messages": messages}


def _fts_query(phrase: str) -> str:
    words = [part for part in re.findall(r"\S+", phrase) if part]
    if not words:
        return '""'
    # Add * suffix for prefix matching, join with AND for multi-word
    # Note: FTS5 prefix terms must NOT be quoted to work
    terms = [f"{word.replace(chr(34), '')}*" for word in words]
    if len(terms) == 1:
        return terms[0]
    return " AND ".join(terms)
