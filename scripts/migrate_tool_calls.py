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
        print(f"Created index")

    con.commit()
    con.close()
    print("Migration complete")


if __name__ == "__main__":
    migrate()