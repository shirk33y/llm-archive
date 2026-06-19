from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from llm_archive import db
from llm_archive.backup import run_backup
from llm_archive.config import AppConfig, config_path, load_config, read_config_text
from llm_archive.embed import auto_embed
from llm_archive.jobs import run_sync_job
from llm_archive.providers import provider_kind, provider_paths

logger = logging.getLogger("llm_archive.service")

SyncRunner = Callable[[str, bool], Awaitable[bool]]

FILE_SYNC_INTERVAL_MS = 10_000
FILE_MIN_SYNC_MS = 10_000
WEB_SYNC_INTERVAL_MS = 1_800_000
WEB_MIN_SYNC_MS = 10_000


class _FileChangeHandler(FileSystemEventHandler):
    def __init__(self, source_id: str, debounce_s: float, callback: Callable[[], None]):
        self.source_id = source_id
        self._debounce_s = debounce_s
        self._callback = callback
        self._last_fire = 0.0
        self._pending = False

    def _mark(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_fire
        if elapsed >= self._debounce_s:
            self._last_fire = now
            self._callback()
            self._pending = False
        else:
            self._pending = True

    def _flush(self) -> None:
        if self._pending:
            self._last_fire = time.monotonic()
            self._callback()
            self._pending = False

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._mark()


async def run_service(
    *,
    runner: SyncRunner,
    db_path: Path | None = None,
    poll_interval: float = 1.0,
) -> None:
    started_at = db.now_ms()

    observer = Observer()
    handlers: dict[str, _FileChangeHandler] = {}
    stale_flags: dict[str, bool] = {}

    def mark_stale(source_id: str) -> None:
        stale_flags[source_id] = True

    def _sync_file_handler(src: str) -> Callable[[], None]:
        def cb() -> None:
            stale_flags[src] = True
        return cb

    config = load_config()
    for source_id in config.ingestors or {}:
        ic = config.ingestor(source_id)
        if not ic.enabled or not ic.watch:
            continue
        if provider_kind(source_id) != "file":
            continue
        paths = [Path(ic.path).expanduser()] if ic.path else list(provider_paths(source_id))
        existing = [p for p in paths if p.exists()]
        for p in existing:
            handler = handlers.get(source_id)
            if handler is None:
                handler = _FileChangeHandler(source_id, FILE_SYNC_INTERVAL_MS / 1000, _sync_file_handler(source_id))
                handlers[source_id] = handler
            observer.schedule(handler, str(p), recursive=True)
    if handlers:
        observer.daemon = True
        observer.start()

    try:
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
                for source_id, flag in list(stale_flags.items()):
                    if flag:
                        stale_flags[source_id] = False
                        for h in handlers.values():
                            if h.source_id == source_id:
                                h._flush()
                                break
                        db.mark_provider_stale(con, source_id)
                await _run_due_syncs(con, config, runner, db_path)
                if config.embed.auto:
                    auto_embed(con)
                await _run_due_backup(con, db_path)
            except Exception:
                logger.exception("service loop error")
            await asyncio.sleep(poll_interval)
    finally:
        if observer.is_alive():
            observer.stop()
            observer.join(timeout=2)


async def _run_due_syncs(con, config: AppConfig, runner: SyncRunner, db_path: Path | None) -> None:
    states = db.provider_states(con)
    for source_id in config.ingestors or {}:
        provider_config = config.ingestor(source_id)
        if not provider_config.enabled:
            continue
        state = states.get(source_id, {})
        is_stale = bool(state.get("stale_since"))
        due_at = state.get("next_sync_at")
        last_success = state.get("last_success_at") or db.get_last_sync(con, source_id)
        if due_at is None and last_success:
            due_at = int(last_success) + (provider_config.sync_interval_ms or 0)
            db.set_provider_next_sync(con, source_id, due_at)
        is_due = due_at is None or int(due_at) <= db.now_ms()
        has_synced = bool(state.get("last_success_at")) or bool(db.get_last_sync(con, source_id))
        watched = provider_config.watch and provider_kind(source_id) == "file"

        if watched and has_synced:
            if not is_stale:
                continue
        elif not is_due and not is_stale:
            continue

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