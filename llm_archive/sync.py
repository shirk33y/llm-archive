from __future__ import annotations
import asyncio
import inspect
import time
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from llm_archive import db
from llm_archive.config import load_config
from llm_archive.ingestors import INGESTORS, get_ingestor
from llm_archive.jobs import run_sync_job

console = Console()
progress_console = Console()


def set_console(c: Console) -> None:
    global console
    console = c


def set_progress_console(c: Console) -> None:
    global progress_console
    progress_console = c


def _run(coro):
    return asyncio.run(coro)


async def _sync_command(
    source: str | None,
    db_path: str | None,
    path: str | None,
    force: bool,
    auth_mode: str | None,
    no_wait: bool,
    json_output: bool,
):
    config = load_config()
    sources = [source] if source else list(INGESTORS)

    async def runner(src: str, job_force: bool) -> bool:
        return await _sync_one(src, db_path, path if src == source else None, job_force, auth_mode)

    results = []
    for src in sources:
        result = await run_sync_job(
            src,
            config=config,
            runner=runner,
            db_path=Path(db_path) if db_path else None,
            force=force,
            wait=not no_wait,
        )
        results.append(result)
        if result.status not in {"success", "joined"}:
            console.print(f"  {src}: {result.reason}")
    if json_output:
        console.print_json(
            data=[
                {
                    "source": item.source_id,
                    "status": item.status,
                    "reason": item.reason,
                    "job_id": item.job_id,
                }
                for item in results
            ]
        )


async def _sync(
    source: str | None,
    db_path_str: str | None,
    path: str | None = None,
    force: bool = False,
    auth_mode: str | None = None,
):
    con = db.connect(Path(db_path_str) if db_path_str else db.DB_PATH)
    sources = [source] if source else list(INGESTORS)

    for src in sources:
        since = None if force else db.get_last_sync(con, src)
        if since is not None and _source_thread_count(con, src) == 0:
            since = None
        ingestor = get_ingestor(src)
        if path and hasattr(ingestor, "path") and src == source:
            ingestor.path = Path(path)
        source_config = {}
        if path and src == source:
            source_config["path"] = path
        if src == source and auth_mode:
            source_config["mode"] = auth_mode
        db.upsert_source(con, src, source_config)
        console.print(f"[bold]Syncing:[/bold] {src}")

        try:
            if since is None:
                await ingestor.init(path=path if src == source else None)
            if not await ingestor.prepare():
                continue
            ok = await _do_ingest(con, ingestor, since=since, force=force)
            if ok:
                db.set_last_sync(con, src, int(time.time() * 1000))
        except Exception as e:
            console.print(f"[red]Error syncing {src}:[/red] {e}")


async def _sync_one(
    source: str,
    db_path_str: str | None,
    path: str | None = None,
    force: bool = False,
    auth_mode: str | None = None,
) -> bool:
    con = db.connect(Path(db_path_str) if db_path_str else db.DB_PATH)
    since = None if force else db.get_last_sync(con, source)
    if since is not None and _source_thread_count(con, source) == 0:
        since = None
    ingestor = get_ingestor(source)
    if path and hasattr(ingestor, "path"):
        ingestor.path = Path(path)
    db.upsert_source(con, source, {"path": path} if path else {})
    console.print(f"[bold]Syncing:[/bold] {source}")
    if since is None:
        await ingestor.init(path=path)
    if not await ingestor.prepare():
        return False
    return await _do_ingest(con, ingestor, since=since, force=force)


def _source_thread_count(con, source: str) -> int:
    row = con.execute("SELECT COUNT(*) FROM threads WHERE source_id=?", (source,)).fetchone()
    return row[0] if row else 0


async def _do_ingest(con, ingestor, since: int | None, force: bool = False):
    written = 0
    updated = 0
    errors = 0
    total = None
    _total_processed = 0

    existing_threads = {}
    if not force:
        rows = con.execute(
            "SELECT id, updated_at FROM threads WHERE source_id=?",
            (ingestor.source_id,),
        ).fetchall()
        existing_threads = {row["id"]: row["updated_at"] for row in rows}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=progress_console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"  {ingestor.source_id}", total=None)

        fast_total = await _ingest_total(ingestor, None)
        if fast_total is not None:
            total = fast_total
            progress.update(task, total=total)

        def _on_total(count: int):
            nonlocal total
            total = count
            progress.update(task, total=total)

        def _on_fetch_start(label: str):
            progress.update(task, description=f"  {ingestor.source_id} — {label}")

        def _desc():
            total_display = total if total is not None else _total_processed
            return (
                f"  {ingestor.source_id} — "
                f"[green]{written}[/green] new, "
                f"[grey37]{updated}[/grey37] updated, "
                f"{total_display} total"
            )

        def _on_fetch_done():
            progress.update(task, description=_desc())

        try:
            sig = inspect.signature(ingestor.threads)
            kwargs = {}
            if "existing_thread_ids" in sig.parameters:
                kwargs["existing_thread_ids"] = existing_threads
            if "on_fetch_start" in sig.parameters:
                kwargs["on_fetch_start"] = _on_fetch_start
            if "on_fetch_done" in sig.parameters:
                kwargs["on_fetch_done"] = _on_fetch_done
            if "on_total" in sig.parameters and total is None:
                kwargs["on_total"] = _on_total

            use_store_thread = "store_thread" in sig.parameters

            if use_store_thread:
                def _store_thread(thread):
                    nonlocal written, updated, _total_processed
                    saved = db.save_thread(con, thread, force=force)
                    _total_processed += 1
                    if saved:
                        if thread.id in existing_threads:
                            updated += 1
                        else:
                            written += 1
                    progress.update(task, advance=1, description=_desc())
                    return saved
                kwargs["store_thread"] = _store_thread

            if "on_skip_timestamps" in sig.parameters:
                def _on_skip_timestamps(updates: dict):
                    nonlocal _total_processed
                    _total_processed += len(updates)
                    db.bulk_update_timestamps(con, updates)
                    progress.update(task, advance=len(updates), description=_desc())
                kwargs["on_skip_timestamps"] = _on_skip_timestamps

            if "on_delta_skip" in sig.parameters:
                def _on_delta_skip(count: int):
                    nonlocal _total_processed
                    _total_processed += count
                    progress.update(task, advance=count, description=_desc())
                kwargs["on_delta_skip"] = _on_delta_skip

            if "tail_check" in sig.parameters:
                def _tail_check(thread) -> bool:
                    return db.check_thread_sha1(con, thread)
                kwargs["tail_check"] = _tail_check

            if kwargs:
                async for thread in ingestor.threads(since=None, **kwargs):
                    if use_store_thread:
                        pass
                    else:
                        thread_force = force
                        is_existing = thread.id in existing_threads
                        if not force and is_existing:
                            db_ts = existing_threads[thread.id]
                            if thread.updated_at and db_ts and thread.updated_at > db_ts:
                                thread_force = True
                        saved = db.save_thread(con, thread, force=thread_force)
                        if saved:
                            if is_existing:
                                updated += 1
                            else:
                                written += 1
                        progress.update(
                            task,
                            advance=1,
                            total=total,
                            description=f"  {ingestor.source_id} — [green]{written}[/green] new, [grey37]{updated}[/grey37] updated",
                        )
            else:
                async for thread in ingestor.threads(since=None):
                    saved = db.save_thread(con, thread, force=force)
                    if saved:
                        written += 1
                    progress.update(
                        task,
                        advance=1,
                        total=total,
                        description=f"  {ingestor.source_id} — [green]{written}[/green] new, [grey37]{updated}[/grey37] updated",
                    )
        except NotImplementedError as e:
            console.print(f"  [yellow]Not implemented:[/yellow] {e}")
            return False
        except RuntimeError as e:
            console.print(f"  [yellow]Warning:[/yellow] {e}")
            return False
        except Exception as e:
            console.print(f"  [red]Error:[/red] {e}")
            errors += 1

    total_shown = total if total is not None else _total_processed
    status = f"[green]{written}[/green] new, [grey37]{updated}[/grey37] updated, {total_shown} total"
    if errors:
        status += f", [red]{errors}[/red] errors"
    console.print(f"  {ingestor.source_id}: {status}")
    return errors == 0


async def _ingest_total(ingestor, since: int | None) -> int | None:
    count = getattr(ingestor, "count_threads", None)
    if not count:
        return None
    try:
        return await count(since=since)
    except Exception as e:
        console.print(f"  [yellow]Count unavailable:[/yellow] {e}")
        return None
