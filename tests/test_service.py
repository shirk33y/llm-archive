from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from llm_archive import db
from llm_archive.config import AppConfig, IngestorConfig
from llm_archive.service import _config_hash, _mark_file_changes, _path_mtime, _run_due_backup, _run_due_syncs


def test_path_mtime_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    mtime = _path_mtime(f)
    assert isinstance(mtime, float)
    assert mtime > 0


def test_path_mtime_directory(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    (d / "a.txt").write_text("a")
    (d / "b.txt").write_text("b")
    mtime = _path_mtime(d)
    assert isinstance(mtime, float)
    assert mtime > 0


def test_path_mtime_nonexistent(tmp_path):
    assert _path_mtime(tmp_path / "nonexistent") is None


def test_path_mtime_oserror_skips_bad_children(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    good = d / "good.txt"
    good.write_text("good")
    bad = d / "bad.txt"
    bad.write_text("bad")

    orig_stat = Path.stat

    def bad_stat(self):
        if self == bad:
            raise OSError("permission denied")
        return orig_stat(self)

    with patch.object(Path, "stat", bad_stat):
        mtime = _path_mtime(d)

    assert isinstance(mtime, float)
    assert mtime > 0


def test_config_hash_reads_config(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("browser_dir = '/tmp/test'")
    with patch("llm_archive.service.config_path", return_value=config_file):
        h = _config_hash()
    expected = hashlib.sha1(b"browser_dir = '/tmp/test'").hexdigest()
    assert h == expected


def test_config_hash_handles_read_error():
    with patch("llm_archive.service.read_config_text", side_effect=OSError("read error")):
        h = _config_hash()
    expected = hashlib.sha1(b"").hexdigest()
    assert h == expected


def _config(*, enabled=True, watch=False, path=None) -> AppConfig:
    return AppConfig(
        ingestors={
            "test_provider": IngestorConfig(
                mode="cookies",
                enabled=enabled,
                sync_interval_ms=60_000,
                min_sync_interval_ms=60_000,
                watch=watch,
                path=path,
            )
        }
    )


@pytest.mark.asyncio
async def test_mark_file_changes_skips_disabled(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    config = _config(enabled=False, watch=True)
    seen = {}
    await _mark_file_changes(con, config, seen)
    assert con.execute("SELECT count(*) FROM provider_state").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_mark_file_changes_skips_unwatched(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    config = _config(enabled=True, watch=False)
    seen = {}
    await _mark_file_changes(con, config, seen)
    assert con.execute("SELECT count(*) FROM provider_state").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_mark_file_changes_no_paths_available(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    config = _config(enabled=True, watch=True, path=str(tmp_path / "nonexistent"))
    seen = {}
    await _mark_file_changes(con, config, seen)
    assert con.execute("SELECT count(*) FROM provider_state").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_mark_file_changes_detects_change(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    con.execute("INSERT INTO sources(id) VALUES ('test_provider')")
    con.commit()

    f = tmp_path / "watched.txt"
    f.write_text("initial")
    config = _config(enabled=True, watch=True, path=str(f))
    seen = {}
    await _mark_file_changes(con, config, seen)

    # First pass: no stale because mtime was first seen
    row = con.execute(
        "SELECT stale_since FROM provider_state WHERE source_id='test_provider'"
    ).fetchone()
    assert row is None

    f.write_text("updated")
    await _mark_file_changes(con, config, seen)
    stale_since = con.execute(
        "SELECT stale_since FROM provider_state WHERE source_id='test_provider'"
    ).fetchone()[0]
    assert stale_since is not None


@pytest.mark.asyncio
async def test_mark_file_changes_skips_when_mtime_unchanged(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    con.execute("INSERT INTO sources(id) VALUES ('test_provider')")
    con.commit()

    f = tmp_path / "watched.txt"
    f.write_text("stable")
    config = _config(enabled=True, watch=True, path=str(f))
    seen = {}

    await _mark_file_changes(con, config, seen)
    row1 = con.execute(
        "SELECT stale_since FROM provider_state WHERE source_id='test_provider'"
    ).fetchone()
    # First pass: no stale yet (just recorded initial mtime)
    assert row1 is None

    await _mark_file_changes(con, config, seen)
    row2 = con.execute(
        "SELECT stale_since FROM provider_state WHERE source_id='test_provider'"
    ).fetchone()
    # Still no stale because mtime hasn't changed
    assert row2 is None


@pytest.mark.asyncio
async def test_run_due_syncs_skips_disabled(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    config = _config(enabled=False)
    runner = AsyncMock()
    await _run_due_syncs(con, config, runner, None)
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_run_due_syncs_triggers_when_stale(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    db.ensure_provider_state(con, "test_provider")
    con.execute(
        "UPDATE provider_state SET stale_since=1 WHERE source_id='test_provider'"
    )
    con.commit()

    config = _config(enabled=True)
    runner = AsyncMock()

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, runner, None)
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_due_syncs_triggers_when_next_sync_passed(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    db.ensure_provider_state(con, "test_provider")
    con.execute(
        "UPDATE provider_state SET next_sync_at=1 WHERE source_id='test_provider'"
    )
    con.commit()

    config = _config(enabled=True)
    runner = AsyncMock()

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, runner, None)
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_due_syncs_triggers_when_no_state(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    config = _config(enabled=True)
    runner = AsyncMock()

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, runner, None)
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_due_syncs_sets_next_sync_from_last_success(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    db.ensure_provider_state(con, "test_provider")
    con.execute(
        "UPDATE provider_state SET last_success_at=5000 WHERE source_id='test_provider'"
    )
    con.commit()

    config = _config(enabled=True)
    runner = AsyncMock()

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, runner, None)
        mock_job.assert_awaited_once()

    row = con.execute(
        "SELECT next_sync_at FROM provider_state WHERE source_id='test_provider'"
    ).fetchone()
    assert row["next_sync_at"] == 5000 + 60000


@pytest.mark.asyncio
async def test_run_due_backup_skips_when_not_due(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    future = db.now_ms() + 86400000 * 2
    con.execute(
        "INSERT INTO backup_state(id, next_backup_at) VALUES (1, ?)", (future,)
    )
    con.commit()

    with patch("llm_archive.service.run_backup") as mock_backup:
        await _run_due_backup(con, None)
        mock_backup.assert_not_called()


@pytest.mark.asyncio
async def test_run_due_backup_runs_when_no_state(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    with patch("llm_archive.service.run_backup") as mock_backup:
        await _run_due_backup(con, None)
        mock_backup.assert_called_once()


@pytest.mark.asyncio
async def test_run_due_backup_runs_when_due(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    past = db.now_ms() - 1000
    con.execute(
        "INSERT INTO backup_state(id, next_backup_at) VALUES (1, ?)", (past,)
    )
    con.commit()

    with patch("llm_archive.service.run_backup") as mock_backup:
        await _run_due_backup(con, None)
        mock_backup.assert_called_once()


@pytest.mark.asyncio
async def test_run_due_backup_records_success(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    with patch("llm_archive.service.run_backup"):
        await _run_due_backup(con, None)

    state = db.get_backup_state(con)
    assert state["next_backup_at"] is not None
    assert state["last_error"] is None


@pytest.mark.asyncio
async def test_run_due_backup_records_failure(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    with patch("llm_archive.service.run_backup", side_effect=RuntimeError("backup failed")):
        await _run_due_backup(con, None)

    state = db.get_backup_state(con)
    assert state["last_error"] is not None
    assert "backup failed" in state["last_error"]
