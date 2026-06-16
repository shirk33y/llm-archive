#!/usr/bin/env python3
"""Migration script to add tool call columns to message_parts table."""

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".llm-archive" / "archive.db"


def migrate():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Check if columns already exist
    cur.execute("PRAGMA table_info(message_parts)")
    columns = {row[1] for row in cur.fetchall()}

    new_columns = [
        ("tool_use_id", "TEXT"),
        ("tool_name", "TEXT"),
        ("tool_input", "TEXT"),
        ("tool_result", "TEXT"),
        ("tool_result_timestamp", "INTEGER"),
        ("tool_is_error", "INTEGER DEFAULT 0"),
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            cur.execute(f"ALTER TABLE message_parts ADD COLUMN {col_name} {col_type}")
            print(f"Added column: {col_name}")
        else:
            print(f"Column already exists: {col_name}")

    # Create indexes for performance
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_tool_use_id ON message_parts(tool_use_id)",
        "CREATE INDEX IF NOT EXISTS idx_tool_name ON message_parts(tool_name)",
        "CREATE INDEX IF NOT EXISTS idx_tool_error ON message_parts(tool_is_error) WHERE tool_is_error = 1",
    ]

    for idx_sql in indexes:
        cur.execute(idx_sql)
        print("Created index")

    con.commit()

    # Backfill tool_name from existing data for local sources
    backfill_tool_names(cur)
    backfill_error_status(cur)

    con.commit()
    con.close()
    print("Migration complete")


def backfill_tool_names(cur: sqlite3.Cursor):
    # Backfill from data.name (windsurf, opencode)
    cur.execute("""
        UPDATE message_parts
        SET tool_name = json_extract(data, '$.name')
        WHERE kind = 'tool_call'
          AND data IS NOT NULL
          AND json_valid(data)
          AND json_extract(data, '$.name') IS NOT NULL
          AND tool_name IS NULL
    """)
    if cur.rowcount:
        print(f"Backfilled {cur.rowcount} tool_name from data.name")

    # Backfill from [Tool: Name] tag (claudecode, codex legacy)
    cur.execute("""
        UPDATE message_parts
        SET tool_name = SUBSTR(text, 8, INSTR(text, ']') - 8)
        WHERE kind = 'tool_call'
          AND text LIKE '[Tool: %]%'
          AND tool_name IS NULL
    """)
    if cur.rowcount:
        print(f"Backfilled {cur.rowcount} tool_name from [Tool: Name] tags")


def backfill_error_status(cur: sqlite3.Cursor):
    # Mark tool results with error content as is_error=1
    cur.execute("""
        UPDATE message_parts
        SET tool_is_error = 1
        WHERE kind = 'tool_result'
          AND (
               text LIKE '%Error%'
            OR text LIKE '%exit code%'
            OR text LIKE '%failed%'
          )
          AND tool_is_error = 0
    """)
    if cur.rowcount:
        print(f"Backfilled {cur.rowcount} tool_is_error=1 from result text")


if __name__ == "__main__":
    migrate()