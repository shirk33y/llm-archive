from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from llm_archive.schema import IngestedThread, IngestedMessage

DB_PATH = Path.home() / ".llm-archive" / "archive.db"

# Tags injected by Claude Code IDE/system that pollute user message content.
# Extend this list as new injection patterns are discovered.
_INJECTION_TAGS = re.compile(
    r'<(?:'
    r'ide_opened_file'
    r'|local-command-caveat'
    r'|command-name'
    r'|command-message'
    r'|command-args'
    r'|system-reminder'
    r'|user-prompt-submit-hook'
    r')[\s\S]*?</[^>]+>',
    re.DOTALL,
)


def clean_content(text: str) -> str:
    """Strip known IDE/system injection tags from message content.

    Raw content is stored in the database; call this at read/display time.
    To add a new tag: extend _INJECTION_TAGS above — applies to all future reads.
    """
    if not text:
        return text
    cleaned = _INJECTION_TAGS.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    last_sync   INTEGER,
    config      TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    id          TEXT PRIMARY KEY,
    source_id   TEXT    NOT NULL,
    title       TEXT,
    created_at  INTEGER,
    updated_at  INTEGER,
    sha1        TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    thread_id   TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  INTEGER,
    metadata    TEXT,
    FOREIGN KEY (thread_id) REFERENCES threads(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread  ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_threads_source   ON threads(source_id);
CREATE INDEX IF NOT EXISTS idx_threads_updated  ON threads(source_id, updated_at);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    for stmt in DDL.split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)
    con.commit()
    return con


def _thread_sha1(thread: IngestedThread) -> str:
    msgs_sorted = sorted(thread.messages, key=lambda m: (m.created_at or 0, m.id))
    payload = thread.id + "".join(m.content for m in msgs_sorted)
    return hashlib.sha1(payload.encode()).hexdigest()


def upsert_source(con: sqlite3.Connection, source_id: str, config: dict) -> None:
    con.execute(
        "INSERT INTO sources(id, config) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
        (source_id, json.dumps(config)),
    )
    con.commit()


def set_last_sync(con: sqlite3.Connection, source_id: str, ts: int) -> None:
    con.execute(
        "INSERT INTO sources(id, last_sync) VALUES(?,?) "
        "ON CONFLICT(id) DO UPDATE SET last_sync=excluded.last_sync",
        (source_id, ts),
    )
    con.commit()


def get_last_sync(con: sqlite3.Connection, source_id: str) -> int | None:
    row = con.execute("SELECT last_sync FROM sources WHERE id=?", (source_id,)).fetchone()
    return row["last_sync"] if row else None


def save_thread(con: sqlite3.Connection, thread: IngestedThread) -> bool:
    """Save thread + messages. Returns True if written, False if skipped (dedup)."""
    sha1 = _thread_sha1(thread)
    existing = con.execute("SELECT sha1 FROM threads WHERE id=?", (thread.id,)).fetchone()
    if existing and existing["sha1"] == sha1:
        return False

    # Ensure source row exists (FK constraint)
    con.execute(
        "INSERT OR IGNORE INTO sources(id) VALUES(?)", (thread.source_id,)
    )

    con.execute(
        "INSERT OR REPLACE INTO threads(id, source_id, title, created_at, updated_at, sha1) "
        "VALUES(?,?,?,?,?,?)",
        (thread.id, thread.source_id, thread.title, thread.created_at, thread.updated_at, sha1),
    )
    for msg in thread.messages:
        con.execute(
            "INSERT OR REPLACE INTO messages(id, thread_id, role, content, created_at, metadata) "
            "VALUES(?,?,?,?,?,?)",
            (msg.id, msg.thread_id, msg.role, msg.content, msg.created_at,
             json.dumps(msg.metadata) if msg.metadata else None),
        )
    con.commit()
    return True


def source_stats(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("""
        SELECT
            s.id,
            s.last_sync,
            s.config,
            COUNT(DISTINCT t.id) AS thread_count,
            COUNT(m.id) AS message_count
        FROM sources s
        LEFT JOIN threads t ON t.source_id = s.id
        LEFT JOIN messages m ON m.thread_id = t.id
        GROUP BY s.id
    """).fetchall()
    return [dict(r) for r in rows]
