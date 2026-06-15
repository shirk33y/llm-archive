from __future__ import annotations

from llm_archive import backup, db


def test_run_backup_copies_and_verifies_database(monkeypatch, tmp_path):
    db_path = tmp_path / "archive.db"
    db.connect(db_path).close()
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path / "backups")

    target = backup.run_backup(db_path, verify=True)

    assert target.exists()
    assert target.parent == tmp_path / "backups"
