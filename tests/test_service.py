from __future__ import annotations

import hashlib
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_archive import db
from llm_archive.config import AppConfig, EmbedConfig, IngestorConfig
from llm_archive.ingestors import get_ingestor
from llm_archive.service import (
    _FileChangeHandler,
    _check_dev_reload,
    _config_hash,
    _path_mtime,
    _run_due_backup,
    _run_due_syncs,
)


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "archive.db")
    yield c
    c.close()


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

    def bad_stat(self, **kwargs):
        if self == bad:
            raise OSError("permission denied")
        return orig_stat(self, **kwargs)

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


def test_file_change_handler_debounce():
    calls = []
    handler = _FileChangeHandler("test", debounce_s=0.05, callback=lambda: calls.append(1))

    handler._mark()
    assert len(calls) == 1

    handler._mark()
    assert len(calls) == 1

    time.sleep(0.06)
    handler._mark()
    assert len(calls) == 2


def test_file_change_handler_flush():
    calls = []
    handler = _FileChangeHandler("test", debounce_s=100, callback=lambda: calls.append(1))
    handler._last_fire = time.monotonic() - 200

    handler._mark()
    assert len(calls) == 1

    handler._mark()
    assert len(calls) == 1

    handler._flush()
    assert len(calls) == 2


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


def test_dummy_requires_test_source_env(monkeypatch):
    with pytest.raises(ValueError, match="Unknown source 'dummy'"):
        get_ingestor("dummy")

    monkeypatch.setenv("LLM_ARCHIVE_ENABLE_TEST_SOURCES", "1")
    assert get_ingestor("dummy").source_id == "dummy"


@pytest.mark.asyncio
async def test_run_due_syncs_skips_disabled(tmp_path, con):
    config = _config(enabled=False)
    runner = AsyncMock()
    await _run_due_syncs(con, config, runner, None)
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_run_due_syncs_skips_dummy_without_test_source_env(tmp_path, con):
    config = AppConfig(
        ingestors={
            "dummy": IngestorConfig(
                enabled=True,
                sync_interval_ms=1_000,
                min_sync_interval_ms=1_000,
                watch=False,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None)
        mock_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_due_syncs_allows_dummy_with_test_source_env(
    tmp_path, con, monkeypatch
):
    monkeypatch.setenv("LLM_ARCHIVE_ENABLE_TEST_SOURCES", "1")
    config = AppConfig(
        ingestors={
            "dummy": IngestorConfig(
                enabled=True,
                sync_interval_ms=1_000,
                min_sync_interval_ms=1_000,
                watch=False,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None)
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_due_syncs_triggers_when_stale(tmp_path, con):
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
async def test_run_due_syncs_triggers_when_next_sync_passed(tmp_path, con):
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
async def test_run_due_syncs_triggers_when_no_state(tmp_path, con):
    config = _config(enabled=True)
    runner = AsyncMock()

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, runner, None)
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_due_syncs_sets_next_sync_from_last_success(tmp_path, con):
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
async def test_run_due_backup_skips_when_not_due(tmp_path, con):
    future = db.now_ms() + 86400000 * 2
    con.execute(
        "INSERT INTO backup_state(id, next_backup_at) VALUES (1, ?)", (future,)
    )
    con.commit()

    with patch("llm_archive.service.run_backup") as mock_backup:
        await _run_due_backup(con, None)
        mock_backup.assert_not_called()


@pytest.mark.asyncio
async def test_run_due_backup_runs_when_no_state(tmp_path, con):
    with patch("llm_archive.service.run_backup") as mock_backup:
        await _run_due_backup(con, None)
        mock_backup.assert_called_once()


@pytest.mark.asyncio
async def test_run_due_backup_runs_when_due(tmp_path, con):
    past = db.now_ms() - 1000
    con.execute(
        "INSERT INTO backup_state(id, next_backup_at) VALUES (1, ?)", (past,)
    )
    con.commit()

    with patch("llm_archive.service.run_backup") as mock_backup:
        await _run_due_backup(con, None)
        mock_backup.assert_called_once()


@pytest.mark.asyncio
async def test_run_due_backup_records_success(tmp_path, con):
    with patch("llm_archive.service.run_backup"):
        await _run_due_backup(con, None)

    state = db.get_backup_state(con)
    assert state["next_backup_at"] is not None
    assert state["last_error"] is None


@pytest.mark.asyncio
async def test_run_due_backup_records_failure(tmp_path, con):
    with patch("llm_archive.service.run_backup", side_effect=RuntimeError("backup failed")):
        await _run_due_backup(con, None)

    state = db.get_backup_state(con)
    assert state["last_error"] is not None
    assert "backup failed" in state["last_error"]


@pytest.mark.asyncio
async def test_watched_provider_skips_timer_when_not_stale(tmp_path, con):
    db.ensure_provider_state(con, "claudecode")
    con.execute(
        "UPDATE provider_state SET last_success_at=5000, next_sync_at=1 WHERE source_id='claudecode'"
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "claudecode": IngestorConfig(
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=10_000,
                watch=True,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None, {"claudecode"})
        mock_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_watched_provider_uses_timer_when_watch_not_active(tmp_path, con):
    db.ensure_provider_state(con, "claudecode")
    con.execute(
        "UPDATE provider_state SET last_success_at=5000, next_sync_at=1 WHERE source_id='claudecode'"
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "claudecode": IngestorConfig(
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=10_000,
                watch=True,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None, set())
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_watched_provider_syncs_when_stale(tmp_path, con):
    db.ensure_provider_state(con, "claudecode")
    db.mark_provider_stale(con, "claudecode")
    con.execute(
        "UPDATE provider_state SET last_success_at=5000, next_sync_at=9999999999999 WHERE source_id='claudecode'"
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "claudecode": IngestorConfig(
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=10_000,
                watch=True,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None)
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_watched_provider_syncs_initial_run(tmp_path, con):

    config = AppConfig(
        ingestors={
            "claudecode": IngestorConfig(
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=10_000,
                watch=True,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None)
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_watched_provider_syncs_on_timer(tmp_path, con):
    db.ensure_provider_state(con, "chatgpt")
    con.execute(
        "UPDATE provider_state SET last_success_at=5000, next_sync_at=1 WHERE source_id='chatgpt'"
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "chatgpt": IngestorConfig(
                mode="cookies",
                enabled=True,
                sync_interval_ms=1_800_000,
                min_sync_interval_ms=1_800_000,
                watch=False,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None)
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_watched_provider_skips_after_successful_sync_not_stale(tmp_path, con):
    db.ensure_provider_state(con, "claudecode")
    recent = db.now_ms()
    con.execute(
        "UPDATE provider_state SET last_success_at=?, next_sync_at=? WHERE source_id='claudecode'",
        (recent, recent + 10_000),
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "claudecode": IngestorConfig(
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=10_000,
                watch=True,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None, {"claudecode"})
        mock_job.assert_not_awaited()

def test_auto_embed_config_default_is_enabled():
    cfg = EmbedConfig()
    assert cfg.auto is True


def test_auto_embed_config_disabled():
    cfg = EmbedConfig(auto=False)
    assert cfg.auto is False


# ── Throttle pre-check tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_throttle_precheck_skips_within_min_interval(tmp_path, con):
    """Watched + stale + has_synced + within min_sync_interval → skipped."""
    db.ensure_provider_state(con, "claudecode")
    recent = db.now_ms()
    con.execute(
        "UPDATE provider_state SET last_success_at=?, next_sync_at=1, stale_since=? WHERE source_id='claudecode'",
        (recent, recent),
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "claudecode": IngestorConfig(
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=30_000,
                watch=True,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None, {"claudecode"})
        mock_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_throttle_precheck_syncs_after_min_interval(tmp_path, con):
    """Watched + stale + has_synced + after min_sync_interval → sync runs."""
    db.ensure_provider_state(con, "claudecode")
    old = db.now_ms() - 60_000  # 60s ago, well past 30s min interval
    con.execute(
        "UPDATE provider_state SET last_success_at=?, next_sync_at=1, stale_since=? WHERE source_id='claudecode'",
        (old, old),
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "claudecode": IngestorConfig(
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=30_000,
                watch=True,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None, {"claudecode"})
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_throttle_precheck_no_last_success_syncs(tmp_path, con):
    """Watched + stale + no last_success → sync runs (no pre-check)."""
    db.ensure_provider_state(con, "claudecode")
    con.execute(
        "UPDATE provider_state SET next_sync_at=1, stale_since=? WHERE source_id='claudecode'",
        (db.now_ms(),),
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "claudecode": IngestorConfig(
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=30_000,
                watch=True,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None, {"claudecode"})
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_throttle_precheck_zero_min_interval_syncs(tmp_path, con):
    """Watched + stale + has_synced + min_sync_interval=0 → sync runs (no throttle)."""
    db.ensure_provider_state(con, "claudecode")
    recent = db.now_ms()
    con.execute(
        "UPDATE provider_state SET last_success_at=?, next_sync_at=1, stale_since=? WHERE source_id='claudecode'",
        (recent, recent),
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "claudecode": IngestorConfig(
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=0,
                watch=True,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None, {"claudecode"})
        mock_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_throttle_precheck_not_watched_syncs(tmp_path, con):
    """Non-watched provider doesn't get throttle pre-check."""
    db.ensure_provider_state(con, "chatgpt")
    recent = db.now_ms()
    con.execute(
        "UPDATE provider_state SET last_success_at=?, next_sync_at=1, stale_since=? WHERE source_id='chatgpt'",
        (recent, recent),
    ).connection.commit()

    config = AppConfig(
        ingestors={
            "chatgpt": IngestorConfig(
                mode="cookies",
                enabled=True,
                sync_interval_ms=10_000,
                min_sync_interval_ms=30_000,
                watch=False,
            )
        }
    )

    with patch("llm_archive.service.run_sync_job", new_callable=AsyncMock) as mock_job:
        await _run_due_syncs(con, config, AsyncMock(), None)
        mock_job.assert_awaited_once()


# ── _check_dev_reload tests ───────────────────────────────────────────────────


def test_dev_reload_none_returns_false(con):
    """dev_mode=None → no reload."""
    observer = MagicMock()
    assert _check_dev_reload(None, con, observer) is False


def test_dev_reload_poll_false_returns_false(con):
    """dev_mode.poll() returns False → no reload."""
    dev_mode = MagicMock()
    dev_mode.poll.return_value = False
    observer = MagicMock()
    assert _check_dev_reload(dev_mode, con, observer) is False


def test_dev_reload_poll_true_idle_reloads(con):
    """dev_mode.poll()=True + no running jobs → clears, stops, reload → True."""
    db.create_job(con, "sync", "claudecode", status="done")
    dev_mode = MagicMock()
    dev_mode.poll.return_value = True
    observer = MagicMock()
    observer.is_alive.return_value = True

    result = _check_dev_reload(dev_mode, con, observer)

    assert result is True
    dev_mode.reload.assert_called_once()
    observer.stop.assert_called_once()


def test_dev_reload_poll_true_busy_waits(con):
    """dev_mode.poll()=True + running jobs → wait (False)."""
    db.create_job(con, "sync", "claudecode", status="running")
    dev_mode = MagicMock()
    dev_mode.poll.return_value = True
    observer = MagicMock()

    result = _check_dev_reload(dev_mode, con, observer)

    assert result is False
    dev_mode.reload.assert_not_called()


def test_dev_reload_observer_dead_still_reloads(con):
    """Observer not alive → doesn't crash, still reloads."""
    dev_mode = MagicMock()
    dev_mode.poll.return_value = True
    observer = MagicMock()
    observer.is_alive.return_value = False

    result = _check_dev_reload(dev_mode, con, observer)

    assert result is True
    dev_mode.reload.assert_called_once()


# ── Export integration tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_sync_job_triggers_export(tmp_path, monkeypatch):
    from llm_archive.jobs import run_sync_job
    from llm_archive.sync import _do_ingest
    from llm_archive.ingestors.dummy import DummyIngestor
    from llm_archive.export import thread_md_path

    monkeypatch.setenv("LLM_ARCHIVE_DUMMY_MARKER", "export-test")
    monkeypatch.setenv("LLM_ARCHIVE_ENABLE_TEST_SOURCES", "1")
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)

    config = AppConfig(
        ingestors={
            "dummy": IngestorConfig(
                mode="cookies",
                enabled=True,
                sync_interval_ms=60_000,
                min_sync_interval_ms=30_000,
            )
        }
    )
    config.export.dir = str(tmp_path / "exports")
    config.export.auto = True

    ingestor = DummyIngestor()

    async def runner(src: str, force: bool) -> bool:
        return await _do_ingest(con, ingestor, since=None, force=force, config=config)

    result = await run_sync_job("dummy", config=config, runner=runner, db_path=db_path)
    assert result.status == "success"

    md_path = thread_md_path("dummy", "dummy:e2e-canary", config)
    assert md_path.exists()
    content = md_path.read_text()
    assert "<!-- thread:dummy:e2e-canary source:dummy -->" in content
    assert "export-test" in content


@pytest.mark.asyncio
async def test_run_sync_job_skips_export_when_disabled(tmp_path, monkeypatch):
    from llm_archive.jobs import run_sync_job
    from llm_archive.sync import _do_ingest
    from llm_archive.ingestors.dummy import DummyIngestor
    from llm_archive.export import thread_md_path

    monkeypatch.setenv("LLM_ARCHIVE_DUMMY_MARKER", "v1")
    monkeypatch.setenv("LLM_ARCHIVE_ENABLE_TEST_SOURCES", "1")
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)

    config = AppConfig(
        ingestors={
            "dummy": IngestorConfig(
                mode="cookies",
                enabled=True,
                sync_interval_ms=60_000,
                min_sync_interval_ms=30_000,
            )
        }
    )
    config.export.dir = str(tmp_path / "exports")
    config.export.auto = False

    ingestor = DummyIngestor()

    async def runner(src: str, force: bool) -> bool:
        return await _do_ingest(con, ingestor, since=None, force=force, config=config)

    result = await run_sync_job("dummy", config=config, runner=runner, db_path=db_path)
    assert result.status == "success"

    md_path = thread_md_path("dummy", "dummy:e2e-canary", config)
    assert not md_path.exists()
