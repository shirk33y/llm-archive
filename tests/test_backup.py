from __future__ import annotations

from pathlib import Path

import pytest

from llm_archive import backup, db


def test_run_backup_copies_and_verifies_database(monkeypatch, tmp_path):
    db_path = tmp_path / "archive.db"
    db.connect(db_path).close()
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path / "backups")

    target = backup.run_backup(db_path, verify=True)

    assert target.exists()
    assert target.parent == tmp_path / "backups"


def test_run_backup_without_verify(monkeypatch, tmp_path):
    db_path = tmp_path / "archive.db"
    db.connect(db_path).close()
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path / "backups")

    target = backup.run_backup(db_path, verify=False)

    assert target.exists()
    assert target.parent == tmp_path / "backups"


def test_run_backup_verify_quick_check(monkeypatch, tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES(1)")
    con.commit()
    con.close()
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path / "backups")

    target = backup.run_backup(db_path, verify=True)
    assert target.exists()

    verifier = __import__("sqlite3").connect(f"file:{target}?mode=ro", uri=True)
    try:
        row = verifier.execute("PRAGMA quick_check").fetchone()
        assert row[0] == "ok"
    finally:
        verifier.close()


def test_run_backup_file_not_found():
    with pytest.raises(FileNotFoundError, match="database not found"):
        backup.run_backup(Path("/nonexistent/path/db.db"))


def test_backup_dir():
    result = backup.backup_dir()
    assert isinstance(result, Path)
    assert ".llm-archive" in result.parts
    assert result.name == "backups"
