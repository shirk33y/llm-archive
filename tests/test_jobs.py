from __future__ import annotations

import asyncio
import logging

import pytest

from llm_archive import db
from llm_archive.config import AppConfig, IngestorConfig
from llm_archive.jobs import ensure_fresh, run_sync_job


def _config(enabled: bool = True) -> AppConfig:
    return AppConfig(
        ingestors={
            "chatgpt": IngestorConfig(
                mode="cookies",
                enabled=enabled,
                sync_interval_ms=60_000,
                min_sync_interval_ms=60_000,
            )
        }
    )


@pytest.mark.asyncio
async def test_sync_job_skips_disabled_provider(tmp_path):
    calls = []

    async def runner(source_id: str, force: bool) -> bool:
        calls.append((source_id, force))
        return True

    result = await run_sync_job(
        "chatgpt",
        config=_config(enabled=False),
        runner=runner,
        db_path=tmp_path / "archive.db",
    )

    assert result.status == "skipped"
    assert result.reason == "disabled"
    assert calls == []


@pytest.mark.asyncio
async def test_sync_job_throttles_recent_success(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    db.set_provider_sync_success(con, "chatgpt", db.now_ms())

    async def runner(source_id: str, force: bool) -> bool:
        raise AssertionError("runner should not be called")

    result = await run_sync_job(
        "chatgpt",
        config=_config(),
        runner=runner,
        db_path=db_path,
    )

    assert result.status == "throttled"
    assert "left" in result.reason


@pytest.mark.asyncio
async def test_sync_job_force_bypasses_throttle(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    db.set_provider_sync_success(con, "chatgpt", db.now_ms())
    calls = []

    async def runner(source_id: str, force: bool) -> bool:
        calls.append((source_id, force))
        return True

    result = await run_sync_job(
        "chatgpt",
        config=_config(),
        runner=runner,
        db_path=db_path,
        force=True,
    )

    assert result.status == "success"
    assert calls == [("chatgpt", True)]


@pytest.mark.asyncio
async def test_sync_job_joins_running_job(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    job_id = db.create_job(con, "sync", "chatgpt")

    async def runner(source_id: str, force: bool) -> bool:
        raise AssertionError("runner should not be called")

    async def finish_job():
        await asyncio.sleep(0.05)
        db.update_job(con, job_id, status="success", finish=True)

    task = asyncio.create_task(finish_job())
    result = await run_sync_job(
        "chatgpt",
        config=_config(),
        runner=runner,
        db_path=db_path,
        wait=True,
    )
    await task

    assert result.status == "joined"
    assert result.waited is True


@pytest.mark.asyncio
async def test_sync_job_reaps_stale_running_job(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    stale_heartbeat = db.now_ms() - db.STALE_JOB_MS - 1
    con.execute(
        "INSERT INTO jobs(kind, source_id, status, reason, started_at, heartbeat_at) VALUES(?, ?, 'running', ?, ?, ?)",
        ("sync", "chatgpt", None, stale_heartbeat, stale_heartbeat),
    )
    con.commit()

    calls = []

    async def runner(source_id: str, force: bool) -> bool:
        calls.append((source_id, force))
        return True

    result = await run_sync_job(
        "chatgpt",
        config=_config(),
        runner=runner,
        db_path=db_path,
    )

    assert result.status == "success"
    assert calls == [("chatgpt", False)]

    stale = con.execute("SELECT status, reason FROM jobs WHERE id=1").fetchone()
    assert stale["status"] == "failed"
    assert stale["reason"] == "stale"


@pytest.mark.asyncio
async def test_sync_job_does_not_reap_fresh_running_job(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    job_id = db.create_job(con, "sync", "chatgpt")

    async def runner(source_id: str, force: bool) -> bool:
        raise AssertionError("runner should not be called")

    async def finish_job():
        await asyncio.sleep(0.05)
        db.update_job(con, job_id, status="success", finish=True)

    task = asyncio.create_task(finish_job())
    result = await run_sync_job(
        "chatgpt",
        config=_config(),
        runner=runner,
        db_path=db_path,
        wait=True,
    )
    await task

    assert result.status == "joined"


@pytest.mark.asyncio
async def test_ensure_fresh_runs_stale_provider(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    db.mark_provider_stale(con, "chatgpt")
    calls = []

    async def runner(source_id: str, force: bool) -> bool:
        calls.append((source_id, force))
        return True

    results = await ensure_fresh(
        ["chatgpt"],
        config=_config(),
        runner=runner,
        db_path=db_path,
    )

    assert [result.status for result in results] == ["success"]
    assert calls == [("chatgpt", False)]
    assert db.provider_states(con)["chatgpt"]["stale_since"] is None


@pytest.mark.asyncio
async def test_sync_job_returns_running_when_no_wait(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    job_id = db.create_job(con, "sync", "chatgpt")

    async def runner(source_id: str, force: bool) -> bool:
        raise AssertionError("runner should not be called")

    result = await run_sync_job(
        "chatgpt",
        config=_config(),
        runner=runner,
        db_path=db_path,
        wait=False,
    )

    assert result.status == "running"
    assert "already syncing" in result.reason
    assert result.job_id == job_id


@pytest.mark.asyncio
async def test_sync_job_logs_wait_message(tmp_path, caplog):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    job_id = db.create_job(con, "sync", "chatgpt")

    async def runner(source_id: str, force: bool) -> bool:
        raise AssertionError("runner should not be called")

    async def finish_job():
        await asyncio.sleep(0.1)
        db.update_job(con, job_id, status="success", finish=True)

    task = asyncio.create_task(finish_job())
    with caplog.at_level(logging.INFO):
        result = await run_sync_job(
            "chatgpt",
            config=_config(),
            runner=runner,
            db_path=db_path,
            wait=True,
        )
    await task

    assert result.status == "joined"
    assert any("waiting for running job" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_wait_for_job_times_out(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    job_id = db.create_job(con, "sync", "chatgpt")

    from llm_archive.jobs import _wait_for_job
    await _wait_for_job(con, job_id, timeout=0.05)

    row = con.execute("SELECT status, reason FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["reason"] == "stale"


def test_reap_stale_jobs_marks_old_jobs_failed(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)

    stale_ts = db.now_ms() - db.STALE_JOB_MS - 1
    con.execute(
        "INSERT INTO jobs(kind, source_id, status, started_at, heartbeat_at) VALUES(?, ?, 'running', ?, ?)",
        ("sync", "chatgpt", stale_ts, stale_ts),
    )
    fresh_ts = db.now_ms()
    con.execute(
        "INSERT INTO jobs(kind, source_id, status, started_at, heartbeat_at) VALUES(?, ?, 'running', ?, ?)",
        ("sync", "claude", fresh_ts, fresh_ts),
    )
    con.commit()

    count = db.reap_stale_jobs(con)
    assert count == 1

    chatgpt = con.execute("SELECT status, reason FROM jobs WHERE source_id='chatgpt'").fetchone()
    assert chatgpt["status"] == "failed"
    assert chatgpt["reason"] == "stale"

    claude = con.execute("SELECT status FROM jobs WHERE source_id='claude'").fetchone()
    assert claude["status"] == "running"


def test_reap_stale_jobs_no_running_jobs(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    assert db.reap_stale_jobs(con) == 0
