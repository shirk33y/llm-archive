from __future__ import annotations
import asyncio
import json
import re
import shutil
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.markdown import Markdown
from rich.text import Text

from llm_archive import db
from llm_archive.backup import run_backup
from llm_archive.config import config_path, format_duration_ms, load_config, read_config_text
from llm_archive.ingestors import INGESTORS, get_ingestor
from llm_archive.jobs import ensure_fresh, run_sync_job
from llm_archive.logging import set_console, set_verbose
from llm_archive.setup import disable_provider, enable_provider, setup_summary

console = Console()
progress_console = Console()


def _run(coro):
    return asyncio.run(coro)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
def main(verbose: bool):
    """llm-archive — dump and sync AI conversations into a local SQLite database."""
    set_console(progress_console)
    set_verbose(verbose)


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS)), required=False)
@click.option("--path", default=None, help="Override local path (for windsurf, etc.)")
@click.option("--db-path", default=None, help="Override database path")
@click.option("-f", "--force", is_flag=True, help="Force full resync (ignore last sync timestamp)")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
@click.option("--auth-mode", type=click.Choice(["cookies", "cdp"]), default=None, help="Override auth mode for this sync")
@click.option("--use-cdp", is_flag=True, help="Use CDP for ChatGPT auth")
@click.option("--no-wait", is_flag=True, help="Do not wait if source already syncing")
@click.option("--json", "json_output", is_flag=True, help="Print JSON")
def sync(
    source: str | None,
    path: str | None,
    db_path: str | None,
    force: bool,
    verbose: bool,
    auth_mode: str | None,
    use_cdp: bool,
    no_wait: bool,
    json_output: bool,
):
    """Sync sources. Performs first-time setup automatically when needed."""
    if verbose:
        set_verbose(True)
    _run(_sync_command(source, db_path, path, force, auth_mode, use_cdp, no_wait, json_output))


async def _sync_command(
    source: str | None,
    db_path: str | None,
    path: str | None,
    force: bool,
    auth_mode: str | None,
    use_cdp: bool,
    no_wait: bool,
    json_output: bool,
):
    config = load_config()
    sources = [source] if source else list(INGESTORS)

    async def runner(src: str, job_force: bool) -> bool:
        return await _sync_one(src, db_path, path if src == source else None, job_force, auth_mode, use_cdp)

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
    use_cdp: bool = False,
):
    con = db.connect(Path(db_path_str) if db_path_str else db.DB_PATH)
    sources = [source] if source else list(INGESTORS)

    for src in sources:
        since = None if force else db.get_last_sync(con, src)
        if since is not None and _source_thread_count(con, src) == 0:
            since = None
        ingestor = get_ingestor(src)
        if src == source and hasattr(ingestor, "_auth_mode") and (auth_mode or use_cdp):
            ingestor._auth_mode = "cdp" if use_cdp else auth_mode
            ingestor._use_cdp = ingestor._auth_mode == "cdp"
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
            # Pre-flight check before progress bar (for interactive setup like CDP)
            if not await ingestor.prepare():
                continue  # Skip this source if not ready
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
    use_cdp: bool = False,
) -> bool:
    con = db.connect(Path(db_path_str) if db_path_str else db.DB_PATH)
    since = None if force else db.get_last_sync(con, source)
    if since is not None and _source_thread_count(con, source) == 0:
        since = None
    ingestor = get_ingestor(source)
    if hasattr(ingestor, "_auth_mode") and (auth_mode or use_cdp):
        ingestor._auth_mode = "cdp" if use_cdp else auth_mode
        ingestor._use_cdp = ingestor._auth_mode == "cdp"
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

    # Fetch existing thread IDs and updated_at for smart sync (disabled when force=True)
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
        # Start with indeterminate bar for immediate visual feedback
        task = progress.add_task(f"  {ingestor.source_id}", total=None)

        # Try fast count_threads (e.g. windsurf just counts .pb files)
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
            # Check if ingestor supports callback parameters
            import inspect

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
                        pass  # saved + progress updated in _store_thread callback
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


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS)))
@click.option("--browser", default=None, help="Detected browser name")
@click.option("--profile", default=None, help="Browser profile name")
@click.option("--browser-path", default=None, help="Custom browser/profile path")
@click.option("--path", default=None, help="Custom file-provider data path")
@click.option("--force", is_flag=True, help="Reconfigure existing source")
@click.option("--dry-run", is_flag=True, help="Detect and verify only")
def enable(
    source: str,
    browser: str | None,
    profile: str | None,
    browser_path: str | None,
    path: str | None,
    force: bool,
    dry_run: bool,
):
    """Configure source, verify auth/path, run first sync."""
    values = enable_provider(
        source,
        browser=browser,
        profile=profile,
        browser_path=browser_path,
        path=path,
        force=force,
        dry_run=dry_run,
    )
    console.print(setup_summary(source, values))
    if not dry_run:
        _run(_sync_command(source, None, path, True, None, False, False, False))


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS)))
@click.option("--no-confirm", is_flag=True, help="Disable without prompt")
def disable(source: str, no_confirm: bool):
    """Disable source scheduling/watchers, keep archived data."""
    if not no_confirm and not click.confirm(f"Disable {source}?", default=True):
        return
    disable_provider(source)
    con = db.connect(db.DB_PATH)
    db.set_provider_enabled(con, source, False)
    console.print(f"{source} disabled")


@main.command()
@click.option("--db-path", default=None, help="Override database path")
@click.option("--verbose", is_flag=True, help="Include setup checks and fix hints")
@click.option("--json", "json_output", is_flag=True, help="Print JSON")
def status(db_path: str | None, verbose: bool, json_output: bool):
    """Show service, provider, auth, backup, and freshness state."""
    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    stats = db.source_stats(con)
    states = db.provider_states(con)
    service_state = db.get_service_state(con)
    backup_state = db.get_backup_state(con)
    jobs = db.recent_jobs(con, 10)

    if json_output:
        console.print_json(
            data={
                "service": service_state,
                "backup": backup_state,
                "providers": list(states.values()),
                "sources": stats,
                "jobs": jobs,
            }
        )
        return

    _print_service_status(service_state)
    _print_backup_status(backup_state)

    if not stats:
        console.print("No sources synced yet. Run `llm-archive enable <source>`.")

    table = Table(title="llm-archive status")
    table.add_column("Source", style="bold")
    table.add_column("State")
    table.add_column("Threads", justify="right")
    table.add_column("Messages", justify="right")
    table.add_column("Last sync")
    table.add_column("Next")
    table.add_column("Notes")

    by_source = {row["id"]: row for row in stats}
    for source_id in sorted(set(by_source) | set(states)):
        row = by_source.get(source_id, {})
        state = states.get(source_id, {})
        last = row.get("last_sync")
        last_str = _fmt_ts(last) if last else "[dim]never[/dim]"
        next_sync = state.get("next_sync_at")
        next_str = _until(next_sync) if next_sync else "-"
        state_str, notes = _provider_status(state)
        table.add_row(
            source_id,
            state_str,
            str(row.get("thread_count", 0)),
            str(row.get("message_count", 0)),
            last_str,
            next_str,
            notes,
        )

    console.print(table)
    if verbose:
        _print_verbose_status(states, jobs)


@main.command()
def sources():
    """List all available sources and whether they have been synced."""
    try:
        con = db.connect(db.DB_PATH)
        initialized = {r["id"] for r in db.source_stats(con)}
    except Exception:
        initialized = set()

    table = Table(title="Available sources")
    table.add_column("Source", style="bold")
    table.add_column("Status")
    table.add_column("Notes")

    notes = {
        "claudecode": "~/.claude/projects/**/*.jsonl",
        "opencode": "~/.local/share/opencode/opencode.db",
        "windsurf": "~/.codeium/windsurf/cascade/ (encrypted, WIP)",
        "claude": "claude.ai REST API (requires Playwright login)",
        "deepseek": "chat.deepseek.com web API (requires Playwright login)",
    }

    for src in INGESTORS:
        status_str = "[green]synced[/green]" if src in initialized else "[dim]not synced[/dim]"
        table.add_row(src, status_str, notes.get(src, ""))

    console.print(table)


@main.command()
@click.argument("phrase")
@click.option("--db-path", default=None, help="Override database path")
@click.option("--limit", default=200, show_default=True, help="Maximum matches to show")
@click.option("--provider", "provider_filter", type=click.Choice(list(INGESTORS)), default=None)
@click.option("--no-refresh", is_flag=True, help="Do not trigger early sync")
@click.option("--json", "json_output", is_flag=True, help="Print JSON")
@click.option(
    "-t", "threads_only", is_flag=True, help="Only show matching threads and match counts"
)
def search(
    phrase: str,
    db_path: str | None,
    limit: int,
    provider_filter: str | None,
    no_refresh: bool,
    json_output: bool,
    threads_only: bool,
):
    """Search all indexed messages across providers."""
    if not no_refresh:
        config = load_config()

        async def runner(src: str, job_force: bool) -> bool:
            return await _sync_one(src, db_path, None, job_force, None, False)

        source_ids = [provider_filter] if provider_filter else list(INGESTORS)
        _run(
            ensure_fresh(
                source_ids,
                config=config,
                runner=runner,
                db_path=Path(db_path) if db_path else None,
            )
        )
    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    rows = (
        db.search_threads(con, phrase, limit=limit)
        if threads_only
        else db.search_messages(con, phrase, limit=limit)
    )
    if provider_filter:
        rows = [row for row in rows if row["source_id"] == provider_filter]
    if json_output:
        console.print_json(data={"query": phrase, "results": rows, "count": len(rows)})
        return
    if not rows:
        console.print("No matches.")
        return
    lines: list[Text | str] = []
    if threads_only:
        formatted_rows = []
        for i, row in enumerate(rows):
            if i:
                formatted_rows.append(None)
            title = row["title"] or "untitled"
            short_id = f"t{db.to_base53(row['thread_rowid'])}"
            rel_time = _relative_time(row["last_match_at"])
            formatted_rows.append(
                {
                    "id": short_id,
                    "time": rel_time,
                    "source": row["source_id"],
                    "text": title,
                    "match_count": row["match_count"],
                }
            )

        max_id_width = max((len(r["id"]) for r in formatted_rows if r is not None), default=0)
        max_time_width = max((len(r["time"]) for r in formatted_rows if r is not None), default=0)

        for row in formatted_rows:
            if row is None:
                lines.append("")
            else:
                lines.append(
                    _search_thread_line(
                        row["id"],
                        row["time"],
                        row["source"],
                        row["text"],
                        max_id_width,
                        max_time_width,
                    )
                )
                lines.append(f"  {row['match_count']} matching messages")
        _print_lines(lines)
        return
    # Pre-build: for each message, find a part that contains the search phrase
    phrase_lower = phrase.lower()
    best_part = {}
    for row in rows:
        mid = row["message_id"]
        if mid not in best_part or phrase_lower in row["content_clean"].lower():
            best_part[mid] = row["content_clean"]

    last = None
    seen_msgs = set()
    formatted_rows = []
    for row in rows:
        title = row["title"] or "untitled"
        key = (row["source_id"], row["thread_id"], title)
        if key != last:
            if last is not None:
                formatted_rows.append(None)
            short_id = f"t{db.to_base53(row['thread_rowid'])}"
            rel_time = _relative_time(row["created_at"])
            formatted_rows.append(
                {
                    "type": "thread",
                    "id": short_id,
                    "time": rel_time,
                    "source": row["source_id"],
                    "text": title,
                }
            )
            last = key
            seen_msgs = set()
        msg_key = row["message_id"]
        if msg_key in seen_msgs:
            continue
        seen_msgs.add(msg_key)
        short_id = f"m{db.to_base53(row['message_rowid'])}"
        rel_time = _relative_time(row["created_at"])
        snippet = _snippet(best_part.get(msg_key, row["content_clean"]), phrase)
        formatted_rows.append(
            {
                "type": "message",
                "id": short_id,
                "time": rel_time,
                "role": row["role"],
                "text": snippet,
                "phrase": phrase,
            }
        )

    max_id_width = max((len(r["id"]) for r in formatted_rows if r is not None), default=0)
    max_time_width = max((len(r["time"]) for r in formatted_rows if r is not None), default=0)

    for row in formatted_rows:
        if row is None:
            lines.append("")
        elif row["type"] == "thread":
            lines.append(
                _search_thread_line(
                    row["id"], row["time"], row["source"], row["text"], max_id_width, max_time_width
                )
            )
        else:
            lines.append(
                _search_message_line(
                    row["id"],
                    row["time"],
                    row["role"],
                    row["text"],
                    row["phrase"],
                    max_id_width,
                    max_time_width,
                )
            )
    _print_lines(lines)


@main.command()
@click.argument("thread")
@click.option("--db-path", default=None, help="Override database path")
def show(thread: str, db_path: str | None):
    """Show a full conversation by ID (short ID like 't5' or full UUID)."""
    con = db.connect(Path(db_path) if db_path else db.DB_PATH)

    row = db.resolve_short_id(con, thread) or db.get_thread(con, thread)

    if row is None:
        console.print(f"Thread not found: {thread}")
        raise SystemExit(1)
    info = row["thread"]
    lines: list[Text | str] = []
    title = info["title"] or "untitled"
    lines.append(_header(info["source_id"], info["id"], title))
    lines.append("")
    for msg in row["messages"]:
        if msg["created_at"]:
            lines.append(_msg_marker_text(msg["created_at"], msg["role"]))
        for part in msg["parts"]:
            if not part["visible"]:
                continue
            data = _part_data(part["data"])
            label = _part_label(part["kind"], data)
            if label:
                lines.append(f"  {label}")
            if part["text"]:
                lines.append(_render_markdown(part["text"]))
        lines.append("")
    _print_lines(lines)


def _relative_time(ms: int) -> str:
    """Format timestamp as relative time (e.g., '1d', '32m', '2y')."""
    from datetime import datetime, timezone

    if not ms:
        return ""
    now = datetime.now(tz=timezone.utc).timestamp() * 1000
    delta_ms = now - ms
    delta_s = delta_ms / 1000
    delta_m = delta_s / 60
    delta_h = delta_m / 60
    delta_d = delta_h / 24
    delta_y = delta_d / 365.25

    if delta_y >= 1:
        return f"{int(delta_y)}y"
    if delta_d >= 1:
        return f"{int(delta_d)}d"
    if delta_h >= 1:
        return f"{int(delta_h)}h"
    if delta_m >= 1:
        return f"{int(delta_m)}m"
    return f"{int(delta_s)}s"


def _search_thread_line(
    short_id: str, rel_time: str, source: str, text: str, id_width: int = 0, time_width: int = 0
) -> Text:
    """Format thread title line with provider name."""
    line = Text()
    line.append(short_id.ljust(id_width), style="grey37")
    line.append(" ", style="")
    line.append(rel_time.ljust(time_width), style="dim yellow")
    line.append(" ", style="")
    line.append(source, style="orange1")
    line.append("  ", style="")
    line.append(_truncate(text, 100), style="bold white")
    return line


def _search_message_line(
    short_id: str,
    rel_time: str,
    role: str,
    text: str,
    phrase: str,
    id_width: int = 0,
    time_width: int = 0,
) -> Text:
    """Format message line with role and highlighted search phrase."""
    line = Text()
    line.append("  ", style="")
    line.append(short_id.ljust(id_width), style="grey37")
    line.append(" ", style="")
    line.append(rel_time.ljust(time_width), style="dim yellow")
    line.append(" ", style="")
    role_style = {"user": "dodger_blue1", "assistant": "green1", "tool": "dark_orange"}.get(
        role, "grey37"
    )
    line.append(role, style=role_style)
    line.append("  ", style="")

    parts = [part for part in re.findall(r"\S+", phrase) if part]
    if not parts:
        line.append(_truncate(text, 100), style="")
        return line

    truncated = _truncate(text, 100)
    text_escaped = escape(truncated)
    for part in parts:
        pattern = re.escape(part)
        text_escaped = re.sub(
            f"({pattern})",
            r"[bold red]\1[/]",
            text_escaped,
            flags=re.IGNORECASE,
        )

    line.append_text(Text.from_markup(text_escaped))
    return line


def _fmt_ts(ms: int) -> str:
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _until(ms: int) -> str:
    remaining = int(ms - time.time() * 1000)
    if remaining <= 0:
        return "due"
    return format_duration_ms(remaining)


def _print_service_status(state: dict | None) -> None:
    if not state or not state.get("heartbeat_at"):
        console.print("service: stopped")
        console.print("hint: brew services start llm-archive")
        return
    age = int(time.time() * 1000) - int(state["heartbeat_at"])
    if age > 90_000:
        console.print(f"service: stale heartbeat {_relative_time(state['heartbeat_at'])} ago")
        console.print("hint: brew services restart llm-archive")
        return
    console.print(f"service: running pid {state.get('pid')}, heartbeat {format_duration_ms(age)} ago")


def _print_backup_status(state: dict | None) -> None:
    if not state:
        console.print("backup: never")
        return
    if state.get("last_error"):
        console.print(f"backup: failed {state['last_error']}")
        return
    last = state.get("last_success_at")
    next_backup = state.get("next_backup_at")
    last_text = _fmt_ts(last) if last else "never"
    next_text = _until(next_backup) if next_backup else "-"
    console.print(f"backup: ok, last {last_text}, next {next_text}")


def _provider_status(state: dict) -> tuple[str, str]:
    if not state:
        return "[dim]unknown[/dim]", ""
    if not state.get("enabled"):
        return "[dim]disabled[/dim]", ""
    if state.get("last_error"):
        return "[red]blocked[/red]", str(state["last_error"])[:80]
    if state.get("stale_since"):
        return "[yellow]stale[/yellow]", f"{state.get('pending_events', 0)} pending"
    return "[green]ok[/green]", ""


def _print_verbose_status(states: dict[str, dict], jobs: list[dict]) -> None:
    if jobs:
        table = Table(title="recent jobs")
        table.add_column("Job")
        table.add_column("Source")
        table.add_column("Status")
        table.add_column("Reason")
        for job in jobs[:10]:
            table.add_row(
                str(job["id"]),
                job.get("source_id") or "-",
                job["status"],
                job.get("reason") or job.get("error") or "",
            )
        console.print(table)
    for source_id, state in states.items():
        if state.get("last_error"):
            console.print(f"{source_id}: fix setup, then run `llm-archive enable {source_id} --force`")


def _msg_marker(ms: int) -> str:
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _msg_marker_text(ms: int, role: str) -> Text:
    text = Text(_msg_marker(ms), style="grey50")
    if role:
        text.append(" ", style="grey50")
        text.append(role, style=_role_style(role))
    return text


def _highlight(text: str, phrase: str) -> str:
    if not text:
        return text
    parts = [part for part in re.findall(r"\S+", phrase) if part]
    if not parts:
        return escape(text)
    text = escape(text)
    for part in parts:
        pattern = re.escape(part)
        text = re.sub(
            f"({pattern})",
            r"[bold red]\1[/]",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _highlight_text(text: str, phrase: str) -> Text:
    return Text.from_markup(_highlight(text, phrase))


def _truncate(text: str, limit: int = 200) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"  # Unicode ellipsis character


def _snippet(text: str, phrase: str, limit: int = 200) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    words = [part for part in re.findall(r"\S+", phrase) if part]
    if not words:
        return _truncate(text, limit)
    lower = text.lower()
    hits = [lower.find(word.lower()) for word in words]
    pos = next((hit for hit in hits if hit >= 0), -1)
    if pos < 0:
        return _truncate(text, limit)
    start = max(0, pos - limit // 3)
    end = min(len(text), start + limit)
    chunk = text[start:end]
    if start > 0:
        chunk = "…" + chunk[1:]
    if end < len(text):
        chunk = chunk[:-1] + "…"
    return chunk


def _local_id(thread_id: str) -> str:
    if ":" not in thread_id:
        return thread_id
    return thread_id.split(":", 1)[1]


def _header(source_id: str, thread_id: str, title: str) -> Text:
    text = Text()
    text.append(f"{source_id}:{_local_id(thread_id)}", style="bold")
    text.append(" ")
    text.append(_truncate(title, 100), style="cyan")
    return text


def _part_data(text: str | None) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def _role_style(role: str) -> str:
    if role == "user":
        return "blue"
    if role == "assistant":
        return "orange3"
    return "bold"


def _render_markdown(text: str) -> Markdown:
    return Markdown(text)


def _part_label(kind: str, data: dict) -> str:
    if kind == "tool_call":
        if data.get("tag"):
            return f"[{data['tag']}]"
        name = data.get("name")
        return f"[Tool: {name}]" if name else "[tool_call]"
    if kind == "tool_result":
        return "[Tool result]"
    if kind == "reasoning":
        return "[Reasoning]"
    if kind == "search_query":
        return "[Search]"
    if kind == "search_result":
        return "[Search results]"
    if kind == "directive":
        return f"[{data.get('tag', 'directive')}]"
    if kind == "status":
        return f"[{data.get('tag', 'status')}]"
    if kind == "citation":
        return f"[{data.get('tag', 'citation')}]"
    return ""


def _print_lines(lines: list[Text | str | Markdown]) -> None:
    def _line_count(item) -> int:
        if isinstance(item, Markdown):
            return 5
        if isinstance(item, Text):
            return item.plain.count("\n") + 1
        return str(item).count("\n") + 1

    count = sum(_line_count(line) for line in lines) or 1
    if not sys.stdout.isatty():
        for line in lines:
            if isinstance(line, Text):
                console.print(line, highlight=False)
            elif isinstance(line, Markdown):
                console.print(line)
            else:
                console.print(line, markup=False, highlight=False)
        return
    height = shutil.get_terminal_size((80, 24)).lines
    if count < max(8, height - 1):
        for line in lines:
            if isinstance(line, Text):
                console.print(line, highlight=False)
            elif isinstance(line, Markdown):
                console.print(line)
            else:
                console.print(line, markup=False, highlight=False)
        return
    with console.pager(styles=True):
        for line in lines:
            if isinstance(line, Text):
                console.print(line, highlight=False)
            elif isinstance(line, Markdown):
                console.print(line)
            else:
                console.print(line, markup=False, highlight=False)


@main.command()
@click.option("--db-path", default=None, help="Override database path")
def tui(db_path: str | None):
    """Interactive TUI for browsing conversations."""
    from llm_archive.tui import run

    run(Path(db_path) if db_path else None)


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS)), required=False)
@click.option("--force", "-f", is_flag=True, help="Re-embed already embedded threads")
@click.option("--model", default="nomic-embed-text", show_default=True, help="Ollama embedding model")
@click.option("--ollama-url", default="http://localhost:11434", show_default=True, help="Ollama API URL")
@click.option("--db-path", default=None, help="Override database path")
def embed(
    source: str | None,
    force: bool,
    model: str,
    ollama_url: str,
    db_path: str | None,
):
    """Generate embeddings for semantic search (requires ollama + embedding model)."""
    import time as _time
    from llm_archive import embed as embed_mod

    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    dims = embed_mod.get_dims(model)
    has_vec = db.init_embeddings(con, dims)
    if not has_vec:
        console.print(
            "[red]sqlite-vec not installed.[/red] Run: [bold]pip install sqlite-vec[/bold]"
        )
        return

    thread_ids = embed_mod.threads_needing_embedding(con, source, force)
    if not thread_ids:
        console.print("All threads already embedded. Use [bold]--force[/bold] to re-embed.")
        return

    console.print(
        f"Embedding [bold]{len(thread_ids)}[/bold] threads using [cyan]{model}[/cyan]..."
    )

    errors = 0
    skipped = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=progress_console,
        transient=True,
    ) as progress:
        task = progress.add_task("Embedding", total=len(thread_ids))
        for thread_id in thread_ids:
            try:
                text = embed_mod.extract_thread_text(con, thread_id)
                if not text.strip():
                    skipped += 1
                    progress.advance(task)
                    continue
                vector = embed_mod.embed_text(text, model, ollama_url)
                blob = embed_mod.serialize(vector)
                db.upsert_thread_embedding(
                    con, thread_id, model, blob, int(_time.time() * 1000)
                )
            except Exception as e:
                errors += 1
                console.print(f"  [red]Error[/red] {thread_id}: {e}")
            progress.advance(task)

    done = len(thread_ids) - errors - skipped
    parts = [f"[green]{done}[/green] embedded"]
    if skipped:
        parts.append(f"[dim]{skipped}[/dim] skipped (empty)")
    if errors:
        parts.append(f"[red]{errors}[/red] errors")
    console.print("  " + ", ".join(parts))


@main.command()
@click.option("--verify", is_flag=True, help="Verify backup after writing")
@click.option("--json", "json_output", is_flag=True, help="Print JSON")
def backup(verify: bool, json_output: bool):
    """Run backup now."""
    con = db.connect(db.DB_PATH)
    try:
        db.set_backup_started(con)
        target = run_backup(verify=verify)
        db.set_backup_success(con, db.now_ms() + 86_400_000)
    except Exception as exc:
        db.set_backup_failure(con, str(exc))
        if json_output:
            console.print_json(data={"status": "failed", "error": str(exc)})
            return
        raise click.ClickException(str(exc))
    if json_output:
        console.print_json(data={"status": "ok", "path": str(target), "verified": verify})
        return
    console.print(f"backup ok: {target}")


@main.command()
def service():
    """Run scheduler process in foreground."""
    from llm_archive.service import run_service

    async def runner(src: str, job_force: bool) -> bool:
        return await _sync_one(src, None, None, job_force, None, False)

    _run(run_service(runner=runner))


@main.group()
def config():
    """View/edit/validate config."""


@config.command("show")
def config_show():
    text = read_config_text()
    console.print(escape(text.rstrip()) if text else "[dim]no config[/dim]")


@config.command("edit")
def config_edit():
    editor = shutil.which("nano") or shutil.which("vi")
    if not editor:
        raise click.ClickException("No editor found")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    import subprocess

    raise SystemExit(subprocess.call([editor, str(path)]))


@config.command("validate")
def config_validate():
    load_config()
    console.print("config ok")


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS)), required=False)
def logs(source: str | None):
    """Show recent service/provider job history."""
    con = db.connect(db.DB_PATH)
    rows = db.recent_jobs(con, 30)
    if source:
        rows = [row for row in rows if row.get("source_id") == source]
    for row in rows:
        console.print(
            f"{row['id']} {row.get('source_id') or '-'} {row['status']} "
            f"{row.get('reason') or row.get('error') or ''}"
        )


@main.command()
def mcp():
    """Start MCP server for conversation search and retrieval (stdio transport)."""
    from llm_archive.mcp_server import run_sync
    run_sync()
