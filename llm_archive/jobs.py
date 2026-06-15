from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from llm_archive import db
from llm_archive.config import AppConfig, format_duration_ms


@dataclass(frozen=True)
class JobResult:
    source_id: str
    status: str
    reason: str
    job_id: int | None = None
    waited: bool = False


SyncRunner = Callable[[str, bool], Awaitable[bool]]


async def run_sync_job(
    source_id: str,
    *,
    config: AppConfig,
    runner: SyncRunner,
    db_path: Path | None = None,
    force: bool = False,
    wait: bool = True,
) -> JobResult:
    con = db.connect(db_path or db.DB_PATH)
    provider_config = config.ingestor(source_id)
    db.ensure_provider_state(con, source_id, provider_config.enabled)

    if not provider_config.enabled and not force:
        job_id = db.create_job(con, "sync", source_id, status="skipped", reason="disabled")
        db.update_job(con, job_id, finish=True)
        return JobResult(source_id, "skipped", "disabled", job_id)

    active = db.active_job(con, "sync", source_id)
    if active:
        if not wait:
            return JobResult(source_id, "running", "already running", active["id"])
        await _wait_for_job(con, active["id"])
        return JobResult(source_id, "joined", "already running, joined", active["id"], True)

    if not force:
        last_success = _last_success_ms(con, source_id)
        minimum = provider_config.min_sync_interval_ms or 0
        remaining = (last_success + minimum) - db.now_ms() if last_success else 0
        if remaining > 0:
            reason = f"throttled {format_duration_ms(remaining)} left"
            job_id = db.create_job(con, "sync", source_id, status="throttled", reason=reason)
            db.update_job(con, job_id, finish=True)
            return JobResult(source_id, "throttled", reason, job_id)

    job_id = db.create_job(con, "sync", source_id, force=force)
    db.set_provider_sync_started(con, source_id)
    try:
        ok = await runner(source_id, force)
    except PermissionError as exc:
        db.set_provider_sync_failure(con, source_id, str(exc), auth_status="failed")
        db.update_job(con, job_id, status="failed", reason="auth_failed", error=str(exc), finish=True)
        return JobResult(source_id, "failed", "auth_failed", job_id)
    except FileNotFoundError as exc:
        db.set_provider_sync_failure(con, source_id, str(exc), path_status="missing")
        db.update_job(con, job_id, status="failed", reason="path_missing", error=str(exc), finish=True)
        return JobResult(source_id, "failed", "path_missing", job_id)
    except Exception as exc:
        db.set_provider_sync_failure(con, source_id, str(exc))
        db.update_job(con, job_id, status="failed", reason="failed", error=str(exc), finish=True)
        return JobResult(source_id, "failed", "failed", job_id)

    if ok:
        ts = db.now_ms()
        db.set_last_sync(con, source_id, ts)
        db.set_provider_sync_success(con, source_id, ts)
        next_sync = ts + (provider_config.sync_interval_ms or 0)
        db.set_provider_next_sync(con, source_id, next_sync)
        db.update_job(con, job_id, status="success", reason="synced", finish=True)
        return JobResult(source_id, "success", "synced", job_id)

    db.set_provider_sync_failure(con, source_id, "sync failed")
    db.update_job(con, job_id, status="failed", reason="failed", error="sync failed", finish=True)
    return JobResult(source_id, "failed", "failed", job_id)


async def ensure_fresh(
    source_ids: list[str],
    *,
    config: AppConfig,
    runner: SyncRunner,
    db_path: Path | None = None,
) -> list[JobResult]:
    con = db.connect(db_path or db.DB_PATH)
    states = db.provider_states(con)
    results = []
    for source_id in source_ids:
        state = states.get(source_id, {})
        if state.get("stale_since") or _due_for_search(state, config.ingestor(source_id)):
            results.append(
                await run_sync_job(
                    source_id,
                    config=config,
                    runner=runner,
                    db_path=db_path,
                    wait=True,
                )
            )
    return results


def _last_success_ms(con, source_id: str) -> int | None:
    states = db.provider_states(con)
    state = states.get(source_id) or {}
    last = state.get("last_success_at") or db.get_last_sync(con, source_id)
    return int(last) if last else None


def _due_for_search(state: dict, provider_config) -> bool:
    last = state.get("last_success_at")
    interval = provider_config.sync_interval_ms
    if not last or not interval:
        return False
    return db.now_ms() - int(last) >= interval


async def _wait_for_job(con, job_id: int) -> None:
    while True:
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "running":
            return
        await asyncio.sleep(0.2)
