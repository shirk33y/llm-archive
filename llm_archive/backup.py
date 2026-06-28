from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from llm_archive import db


def backup_dir() -> Path:
    return Path.home() / ".llm-archive" / "backups"


def run_backup(db_path: Path | None = None, *, verify: bool = False) -> Path:
    source = db_path or db.DB_PATH
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"archive-{stamp}.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    if verify:
        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
    return target
