from __future__ import annotations
import hashlib
import json
import os
import re
import socket
import sqlite3
import time
from pathlib import Path

from llm_archive.ids import from_base53
from llm_archive.logging import get_logger
from llm_archive.schema import IngestedPart, IngestedThread, IngestedMessage
from llm_archive.text import (_parse_parts, _strip_content,
                               clean_content)
from llm_archive.unicode import sanitize_text

logger = get_logger("db")

DB_PATH = Path(
    os.environ.get("LLM_ARCHIVE_DB", Path.home() / ".llm-archive" / "archive.db")
)


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

CREATE TABLE IF NOT EXISTS provider_state (
    source_id             TEXT PRIMARY KEY,
    enabled               INTEGER NOT NULL DEFAULT 0,
    stale_since           INTEGER,
    pending_events        INTEGER NOT NULL DEFAULT 0,
    last_sync_started_at  INTEGER,
    last_sync_finished_at INTEGER,
    last_success_at       INTEGER,
    next_sync_at          INTEGER,
    last_error            TEXT,
    failure_count         INTEGER NOT NULL DEFAULT 0,
    auth_status           TEXT,
    path_status           TEXT,
    watch_active          INTEGER NOT NULL DEFAULT 0,
    watch_seen_at         INTEGER,
    watch_error           TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    source_id       TEXT,
    status          TEXT NOT NULL,
    reason          TEXT,
    started_at      INTEGER NOT NULL,
    heartbeat_at    INTEGER NOT NULL,
    finished_at     INTEGER,
    force           INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(kind, source_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_history ON jobs(started_at DESC);

CREATE TABLE IF NOT EXISTS service_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    pid            INTEGER,
    started_at     INTEGER,
    heartbeat_at   INTEGER,
    version        TEXT,
    config_hash    TEXT
);

CREATE TABLE IF NOT EXISTS backup_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_started_at INTEGER,
    last_success_at INTEGER,
    next_backup_at  INTEGER,
    last_error      TEXT,
    failure_count   INTEGER NOT NULL DEFAULT 0
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
    tool_use_id      TEXT,
    tool_name        TEXT,
    tool_input       TEXT,
    tool_result      TEXT,
    tool_result_timestamp INTEGER,
    tool_is_error    INTEGER DEFAULT 0,
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


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    for stmt in DDL.split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)
    _ensure_column(con, "provider_state", "watch_active", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(con, "provider_state", "watch_seen_at", "INTEGER")
    _ensure_column(con, "provider_state", "watch_error", "TEXT")
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


def _ensure_column(
    con: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _thread_sha1(thread: IngestedThread) -> str:
    msgs_sorted = sorted(thread.messages, key=lambda m: (m.created_at or 0, m.id))
    payload = thread.id + "".join(sanitize_text(m.content) for m in msgs_sorted)
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
            logger.debug(f"sha1 match {thread.id} — skipping")
        else:
            logger.debug(f"sha1 changed {thread.id} — old={existing['sha1']} new={sha1}")
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
        (
            thread.id,
            thread.source_id,
            sanitize_text(thread.title) if thread.title else thread.title,
            thread.created_at,
            thread.updated_at,
            sha1,
            now_ms,
        ),
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
        content = sanitize_text(msg.content)
        clean = clean_content(content)
        con.execute(
            "INSERT OR REPLACE INTO messages(id, thread_id, role, content, content_clean, created_at, metadata) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                msg.id,
                msg.thread_id,
                msg.role,
                content,
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
            text = sanitize_text(_strip_content(part.text).strip())
            search_text = clean_content(text) if part.searchable else ""
            # Also add data field content to search_text for better searchability
            if part.data and part.searchable:
                data_str = json.dumps(part.data, ensure_ascii=False)
                search_text += " " + clean_content(data_str)
            # Add tool call data to search_text
            if part.tool_call and part.searchable:
                tc = part.tool_call
                search_text += f" {sanitize_text(tc.name)}"
                if tc.input:
                    search_text += f" {sanitize_text(json.dumps(tc.input, ensure_ascii=False))}"
                if tc.result:
                    search_text += f" {sanitize_text(tc.result)}"
            tool_use_id = part.tool_call.tool_use_id if part.tool_call else None
            tool_name = sanitize_text(part.tool_call.name) if part.tool_call else None
            tool_input = json.dumps(part.tool_call.input) if part.tool_call and part.tool_call.input else None
            tool_result = sanitize_text(part.tool_call.result) if part.tool_call and part.tool_call.result else None
            tool_result_ts = part.tool_call.resultTimestamp if part.tool_call else None
            tool_is_error = 1 if part.tool_call and part.tool_call.is_error else 0
            con.execute(
                "INSERT OR REPLACE INTO message_parts(message_id, ord, kind, text, search_text, data, visible, searchable, tool_use_id, tool_name, tool_input, tool_result, tool_result_timestamp, tool_is_error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    msg.id,
                    i,
                    part.kind,
                    text,
                    search_text,
                    json.dumps(part.data) if part.data else None,
                    1 if part.visible else 0,
                    1 if part.searchable else 0,
                    tool_use_id,
                    tool_name,
                    tool_input,
                    tool_result,
                    tool_result_ts,
                    tool_is_error,
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


def source_sizes(con: sqlite3.Connection) -> dict[str, int]:
    """Approximate DB storage (chars) per source from message text columns."""
    rows = con.execute(
        """
        SELECT source_id, SUM(sz) AS size FROM (
            SELECT t.source_id,
                   SUM(COALESCE(LENGTH(m.content),0) + COALESCE(LENGTH(m.content_clean),0)) AS sz
            FROM threads t JOIN messages m ON m.thread_id = t.id
            GROUP BY t.source_id
            UNION ALL
            SELECT t.source_id, SUM(
                COALESCE(LENGTH(mp.text),0) + COALESCE(LENGTH(mp.search_text),0) +
                COALESCE(LENGTH(mp.data),0) + COALESCE(LENGTH(mp.tool_input),0) +
                COALESCE(LENGTH(mp.tool_result),0)
            ) AS sz
            FROM threads t JOIN messages m ON m.thread_id = t.id
            JOIN message_parts mp ON mp.message_id = m.id
            GROUP BY t.source_id
            UNION ALL
            SELECT t.source_id, SUM(COALESCE(LENGTH(mr.raw),0)) AS sz
            FROM threads t JOIN messages m ON m.thread_id = t.id
            JOIN message_raw mr ON mr.message_id = m.id
            GROUP BY t.source_id
        ) GROUP BY source_id
        """
    ).fetchall()
    return {row["source_id"]: row["size"] or 0 for row in rows}


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_provider_state(con: sqlite3.Connection, source_id: str, enabled: bool = False) -> None:
    con.execute(
        "INSERT OR IGNORE INTO provider_state(source_id, enabled) VALUES(?, ?)",
        (source_id, 1 if enabled else 0),
    )
    con.commit()


def set_provider_enabled(con: sqlite3.Connection, source_id: str, enabled: bool) -> None:
    ensure_provider_state(con, source_id, enabled)
    con.execute(
        "UPDATE provider_state SET enabled=? WHERE source_id=?",
        (1 if enabled else 0, source_id),
    )
    con.commit()


def mark_provider_stale(con: sqlite3.Connection, source_id: str) -> None:
    ts = now_ms()
    ensure_provider_state(con, source_id)
    con.execute(
        """
        UPDATE provider_state
        SET stale_since=COALESCE(stale_since, ?), pending_events=pending_events + 1
        WHERE source_id=?
        """,
        (ts, source_id),
    )
    con.commit()


def set_provider_next_sync(con: sqlite3.Connection, source_id: str, ts: int | None) -> None:
    ensure_provider_state(con, source_id)
    con.execute("UPDATE provider_state SET next_sync_at=? WHERE source_id=?", (ts, source_id))
    con.commit()


def set_provider_sync_started(con: sqlite3.Connection, source_id: str, ts: int | None = None) -> None:
    ensure_provider_state(con, source_id)
    con.execute(
        "UPDATE provider_state SET last_sync_started_at=? WHERE source_id=?",
        (ts or now_ms(), source_id),
    )
    con.commit()


def set_provider_sync_success(con: sqlite3.Connection, source_id: str, ts: int | None = None) -> None:
    ts = ts or now_ms()
    ensure_provider_state(con, source_id)
    con.execute(
        """
        UPDATE provider_state
        SET last_sync_finished_at=?, last_success_at=?, stale_since=NULL, pending_events=0,
            last_error=NULL, failure_count=0, auth_status='ok', path_status='ok'
        WHERE source_id=?
        """,
        (ts, ts, source_id),
    )
    con.commit()


def set_provider_sync_failure(
    con: sqlite3.Connection,
    source_id: str,
    error: str,
    *,
    auth_status: str | None = None,
    path_status: str | None = None,
) -> None:
    ensure_provider_state(con, source_id)
    con.execute(
        """
        UPDATE provider_state
        SET last_sync_finished_at=?, last_error=?, failure_count=failure_count + 1,
            auth_status=COALESCE(?, auth_status), path_status=COALESCE(?, path_status)
        WHERE source_id=?
        """,
        (now_ms(), error[:500], auth_status, path_status, source_id),
    )
    con.commit()


def set_provider_watch_status(
    con: sqlite3.Connection,
    source_id: str,
    *,
    active: bool,
    error: str | None = None,
) -> None:
    ensure_provider_state(con, source_id)
    con.execute(
        """
        UPDATE provider_state
        SET watch_active=?, watch_seen_at=?, watch_error=?
        WHERE source_id=?
        """,
        (1 if active else 0, now_ms() if active else None, error, source_id),
    )
    con.commit()


def provider_states(con: sqlite3.Connection) -> dict[str, dict]:
    rows = con.execute("SELECT * FROM provider_state ORDER BY source_id").fetchall()
    return {row["source_id"]: dict(row) for row in rows}


def active_job(con: sqlite3.Connection, kind: str, source_id: str | None) -> dict | None:
    reap_stale_jobs(con)
    row = con.execute(
        """
        SELECT * FROM jobs
        WHERE kind=? AND (source_id=? OR (source_id IS NULL AND ? IS NULL)) AND status='running'
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (kind, source_id, source_id),
    ).fetchone()
    return dict(row) if row else None


STALE_JOB_MS = 5 * 60 * 1000


def reap_stale_jobs(con: sqlite3.Connection) -> int:
    """Mark running jobs with no heartbeat for > 5 minutes as failed."""
    threshold = now_ms() - STALE_JOB_MS
    cur = con.execute(
        """
        UPDATE jobs SET status='failed', reason='stale', finished_at=?
        WHERE status='running' AND heartbeat_at < ?
        """,
        (now_ms(), threshold),
    )
    con.commit()
    return cur.rowcount


def clear_running_jobs(con: sqlite3.Connection) -> int:
    """Cancel all running jobs. Used before dev-mode reexec to avoid orphans."""
    cur = con.execute(
        "UPDATE jobs SET status='cancelled', finished_at=? WHERE status='running'",
        (now_ms(),),
    )
    con.commit()
    return cur.rowcount


def create_job(
    con: sqlite3.Connection,
    kind: str,
    source_id: str | None,
    *,
    force: bool = False,
    status: str = "running",
    reason: str | None = None,
) -> int:
    ts = now_ms()
    cur = con.execute(
        """
        INSERT INTO jobs(kind, source_id, status, reason, started_at, heartbeat_at, force)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (kind, source_id, status, reason, ts, ts, 1 if force else 0),
    )
    con.commit()
    if cur.lastrowid is None:
        raise RuntimeError("failed to create job")
    return cur.lastrowid


def update_job(
    con: sqlite3.Connection,
    job_id: int,
    *,
    status: str | None = None,
    reason: str | None = None,
    error: str | None = None,
    finish: bool = False,
) -> None:
    fields = ["heartbeat_at=?"]
    params: list = [now_ms()]
    if status is not None:
        fields.append("status=?")
        params.append(status)
    if reason is not None:
        fields.append("reason=?")
        params.append(reason)
    if error is not None:
        fields.append("error=?")
        params.append(error[:500])
    if finish:
        fields.append("finished_at=?")
        params.append(now_ms())
    params.append(job_id)
    con.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", params)
    con.commit()


def recent_jobs(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = con.execute("SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def heartbeat_service(
    con: sqlite3.Connection,
    *,
    pid: int,
    started_at: int,
    version: str,
    config_hash: str,
) -> None:
    con.execute(
        """
        INSERT INTO service_state(id, pid, started_at, heartbeat_at, version, config_hash)
        VALUES(1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            pid=excluded.pid,
            started_at=excluded.started_at,
            heartbeat_at=excluded.heartbeat_at,
            version=excluded.version,
            config_hash=excluded.config_hash
        """,
        (pid, started_at, now_ms(), version, config_hash),
    )
    con.commit()


def get_service_state(con: sqlite3.Connection) -> dict | None:
    row = con.execute("SELECT * FROM service_state WHERE id=1").fetchone()
    return dict(row) if row else None


def set_backup_started(con: sqlite3.Connection) -> None:
    con.execute(
        """
        INSERT INTO backup_state(id, last_started_at) VALUES(1, ?)
        ON CONFLICT(id) DO UPDATE SET last_started_at=excluded.last_started_at
        """,
        (now_ms(),),
    )
    con.commit()


def set_backup_success(con: sqlite3.Connection, next_backup_at: int | None = None) -> None:
    ts = now_ms()
    con.execute(
        """
        INSERT INTO backup_state(id, last_success_at, next_backup_at, last_error, failure_count)
        VALUES(1, ?, ?, NULL, 0)
        ON CONFLICT(id) DO UPDATE SET
            last_success_at=excluded.last_success_at,
            next_backup_at=excluded.next_backup_at,
            last_error=NULL,
            failure_count=0
        """,
        (ts, next_backup_at),
    )
    con.commit()


def set_backup_failure(con: sqlite3.Connection, error: str) -> None:
    con.execute(
        """
        INSERT INTO backup_state(id, last_error, failure_count) VALUES(1, ?, 1)
        ON CONFLICT(id) DO UPDATE SET
            last_error=excluded.last_error,
            failure_count=failure_count + 1
        """,
        (error[:500],),
    )
    con.commit()


def get_backup_state(con: sqlite3.Connection) -> dict | None:
    row = con.execute("SELECT * FROM backup_state WHERE id=1").fetchone()
    return dict(row) if row else None


def _load_vec(con: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        return True
    except Exception:
        return False


_EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS thread_embeddings (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT UNIQUE NOT NULL,
    model       TEXT NOT NULL,
    embedded_at INTEGER NOT NULL
);
"""


def init_embeddings(con: sqlite3.Connection, dims: int = 384) -> tuple[bool, bool]:
    """Initialize embedding tables.

    Returns (has_vec, dim_mismatch). dim_mismatch is True when an existing
    vec_threads table uses different dimensions — the caller must decide
    whether to rebuild (requires --force) or abort.
    """
    has_vec = _load_vec(con)
    con.execute(_EMBEDDINGS_DDL)
    con.commit()
    dim_mismatch = False
    if has_vec:
        existing = con.execute(
            "SELECT sql FROM sqlite_master WHERE name='vec_threads'"
        ).fetchone()
        if existing and f"FLOAT[{dims}]" not in existing[0]:
            dim_mismatch = True
        try:
            con.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_threads USING vec0(embedding FLOAT[{dims}])"
            )
            con.commit()
        except Exception:
            pass
    return has_vec, dim_mismatch


def has_embeddings(con: sqlite3.Connection) -> bool:
    row = con.execute("SELECT COUNT(*) FROM thread_embeddings").fetchone()
    return row[0] > 0


def upsert_thread_embedding(
    con: sqlite3.Connection,
    thread_id: str,
    model: str,
    vector_bytes: bytes,
    embedded_at: int,
) -> None:
    con.execute(
        """
        INSERT INTO thread_embeddings(thread_id, model, embedded_at)
        VALUES (?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET model=excluded.model, embedded_at=excluded.embedded_at
        """,
        (thread_id, model, embedded_at),
    )
    rowid = con.execute(
        "SELECT rowid FROM thread_embeddings WHERE thread_id=?", (thread_id,)
    ).fetchone()["rowid"]
    con.execute("DELETE FROM vec_threads WHERE rowid=?", (rowid,))
    con.execute("INSERT INTO vec_threads(rowid, embedding) VALUES (?, ?)", (rowid, vector_bytes))
    con.commit()


_SUMMARIES_DDL = """
CREATE TABLE IF NOT EXISTS thread_summaries (
    thread_id   TEXT PRIMARY KEY,
    tiny        TEXT,
    small       TEXT,
    medium      TEXT,
    large       TEXT,
    model       TEXT NOT NULL,
    summarized_at INTEGER NOT NULL
);
"""


def init_summaries(con: sqlite3.Connection) -> None:
    con.execute(_SUMMARIES_DDL)
    con.commit()


def upsert_thread_summary(
    con: sqlite3.Connection,
    thread_id: str,
    tiny: str,
    small: str,
    medium: str,
    large: str,
    model: str,
    summarized_at: int,
) -> None:
    con.execute(
        """
        INSERT INTO thread_summaries(thread_id, tiny, small, medium, large, model, summarized_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            tiny=excluded.tiny, small=excluded.small, medium=excluded.medium,
            large=excluded.large, model=excluded.model, summarized_at=excluded.summarized_at
        """,
        (thread_id, tiny, small, medium, large, model, summarized_at),
    )
    con.commit()


def threads_needing_summary(
    con: sqlite3.Connection,
    source_id: str | None = None,
    force: bool = False,
    min_new_messages: int = 3,
) -> list[dict]:
    init_summaries(con)
    if force:
        q = "SELECT id FROM threads" + (" WHERE source_id=?" if source_id else "")
        rows = con.execute(q, (source_id,) if source_id else ()).fetchall()
    else:
        q = """
            SELECT t.id, COUNT(m.id) AS new_msg_count
            FROM threads t
            LEFT JOIN thread_summaries ts ON ts.thread_id = t.id
            LEFT JOIN messages m ON m.thread_id = t.id
                AND (ts.summarized_at IS NULL OR m.created_at > ts.summarized_at)
            WHERE ts.summarized_at IS NULL
               OR EXISTS (
                   SELECT 1 FROM messages m2
                   WHERE m2.thread_id = t.id
                     AND m2.created_at > ts.summarized_at
               )
        """
        params: list = []
        if source_id:
            q += " AND t.source_id = ?"
            params.append(source_id)
        q += " GROUP BY t.id HAVING new_msg_count >= ?"
        params.append(min_new_messages)
        rows = con.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_thread_summary(con: sqlite3.Connection, thread_id: str) -> dict | None:
    init_summaries(con)
    row = con.execute(
        "SELECT * FROM thread_summaries WHERE thread_id=?", (thread_id,)
    ).fetchone()
    return dict(row) if row else None


def semantic_search_threads(
    con: sqlite3.Connection,
    query_vector: bytes,
    limit: int | None = None,
    source_id: str | None = None,
) -> list[dict]:
    _load_vec(con)
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE name='vec_threads'").fetchone()
    if not exists:
        return []
    k = min(limit if limit is not None else 200, 4096)
    if source_id:
        rows = con.execute(
            """
            SELECT te.thread_id, v.distance, t.title, t.source_id, t.updated_at,
                   t.rowid AS thread_rowid
            FROM vec_threads v
            JOIN thread_embeddings te ON te.rowid = v.rowid
            JOIN threads t ON t.id = te.thread_id
            WHERE v.embedding MATCH ? AND k = ? AND t.source_id = ?
            ORDER BY v.distance
            """,
            (query_vector, k, source_id),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT te.thread_id, v.distance, t.title, t.source_id, t.updated_at,
                   t.rowid AS thread_rowid
            FROM vec_threads v
            JOIN thread_embeddings te ON te.rowid = v.rowid
            JOIN threads t ON t.id = te.thread_id
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (query_vector, k),
        ).fetchall()
    return [dict(r) for r in rows]


def search_messages(con: sqlite3.Connection, phrase: str, limit: int | None = None) -> list[dict]:
    # First find matching message IDs (limit applies to distinct messages)
    # Then join parts for content — avoids LIMIT cutting off mid-message
    limit_clause = f"LIMIT {limit}" if limit is not None else ""
    rows = con.execute(
        f"""
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
            {limit_clause}
        ) matched
        JOIN messages m ON m.id = matched.id
        JOIN threads t ON t.id = m.thread_id
        JOIN message_parts p ON p.message_id = m.id
        ORDER BY t.updated_at DESC, m.created_at DESC, m.id, p.ord
        """,
        (_fts_query(phrase),),
    ).fetchall()
    return [dict(r) for r in rows]


def search_threads(con: sqlite3.Connection, phrase: str, limit: int | None = None) -> list[dict]:
    limit_clause = f"LIMIT {limit}" if limit is not None else ""
    rows = con.execute(
        f"""
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
        {limit_clause}
        """,
        (_fts_query(phrase),),
    ).fetchall()
    return [dict(r) for r in rows]


def list_threads(con: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Return all threads sorted by newest first."""
    limit_clause = f"LIMIT {limit}" if limit is not None else ""
    rows = con.execute(
        f"""
        SELECT
            source_id,
            id AS thread_id,
            title,
            updated_at,
            rowid AS thread_rowid
        FROM threads
        ORDER BY updated_at DESC
        {limit_clause}
        """,
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
            "SELECT message_id, ord, kind, text, data, visible, searchable, tool_name, tool_input, tool_result, tool_is_error FROM message_parts "
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
