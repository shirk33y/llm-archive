from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

from llm_archive import db
from llm_archive.backup import run_backup
from llm_archive.config import AppConfig, config_path, load_config, read_config_text
from llm_archive.jobs import run_sync_job
from llm_archive.providers import provider_paths

logger = logging.getLogger("llm_archive.service")

SyncRunner = Callable[[str, bool], Awaitable[bool]]


async def run_service(
    *,
    runner: SyncRunner,
    db_path: Path | None = None,
    poll_interval: float = 1.0,
) -> None:
    started_at = db.now_ms()
    seen_mtimes: dict[str, float] = {}
    while True:
        try:
            config = load_config()
            con = db.connect(db_path or db.DB_PATH)
            db.heartbeat_service(
                con,
                pid=os.getpid(),
                started_at=started_at,
                version="0.1.0",
                config_hash=_config_hash(),
            )
            await _mark_file_changes(con, config, seen_mtimes)
            await _run_due_syncs(con, config, runner, db_path)
            await _run_due_backup(con, db_path)
        except Exception:
            logger.exception("service loop error")
        await asyncio.sleep(poll_interval)


async def _mark_file_changes(con, config: AppConfig, seen_mtimes: dict[str, float]) -> None:
    for source_id in config.ingestors or {}:
        full_config = config.ingestor(source_id)
        if not full_config.enabled or not full_config.watch:
            continue
        paths = [Path(full_config.path).expanduser()] if full_config.path else list(provider_paths(source_id))
        for path in paths:
            mtime = _path_mtime(path)
            if mtime is None:
                continue
            key = f"{source_id}:{path}"
            previous = seen_mtimes.setdefault(key, mtime)
            if mtime > previous:
                seen_mtimes[key] = mtime
                db.mark_provider_stale(con, source_id)


async def _run_due_syncs(con, config: AppConfig, runner: SyncRunner, db_path: Path | None) -> None:
    states = db.provider_states(con)
    for source_id in config.ingestors or {}:
        provider_config = config.ingestor(source_id)
        if not provider_config.enabled:
            continue
        state = states.get(source_id, {})
        due_at = state.get("next_sync_at")
        last_success = state.get("last_success_at") or db.get_last_sync(con, source_id)
        if due_at is None and last_success:
            due_at = int(last_success) + (provider_config.sync_interval_ms or 0)
            db.set_provider_next_sync(con, source_id, due_at)
        if state.get("stale_since") or due_at is None or int(due_at) <= db.now_ms():
            await run_sync_job(
                source_id,
                config=config,
                runner=runner,
                db_path=db_path,
                wait=False,
            )


async def _run_due_backup(con, db_path: Path | None) -> None:
    state = db.get_backup_state(con) or {}
    next_backup_at = state.get("next_backup_at")
    day_ms = 86_400_000
    if next_backup_at is not None and int(next_backup_at) > db.now_ms():
        return
    try:
        db.set_backup_started(con)
        run_backup(db_path, verify=False)
        db.set_backup_success(con, db.now_ms() + day_ms)
    except Exception as exc:
        db.set_backup_failure(con, str(exc))


def _path_mtime(path: Path) -> float | None:
    if not path.exists():
        return None
    if path.is_file():
        return path.stat().st_mtime
    newest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            if child.is_file():
                newest = max(newest, child.stat().st_mtime)
        except OSError:
            pass
    return newest


def _config_hash() -> str:
    try:
        text = read_config_text(config_path())
    except Exception:
        text = ""
    return hashlib.sha1(text.encode()).hexdigest()
