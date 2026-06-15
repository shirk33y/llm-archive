from __future__ import annotations

import asyncio

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
