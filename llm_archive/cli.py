from __future__ import annotations
import asyncio
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from llm_archive import db
from llm_archive.registry import INGESTORS, get_ingestor

console = Console()


def _run(coro):
    return asyncio.run(coro)


@click.group()
def main():
    """llm-archive — dump and sync AI conversations into a local SQLite database."""
    pass


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS)))
@click.option("--path", default=None, help="Override local path (for windsurf, etc.)")
@click.option("--db-path", default=None, help="Override database path")
def init(source: str, path: str | None, db_path: str | None):
    """First-time setup for a source. Imports all available data."""
    _run(_init(source, path, db_path))


async def _init(source: str, path: str | None, db_path_str: str | None):
    con = db.connect(Path(db_path_str) if db_path_str else db.DB_PATH)
    ingestor = get_ingestor(source)

    if path and hasattr(ingestor, "path"):
        ingestor.path = Path(path)

    db.upsert_source(con, source, {"path": path} if path else {})

    console.print(f"[bold]Initializing source:[/bold] {source}")

    try:
        await ingestor.init(path=path)
    except Exception as e:
        console.print(f"[red]Init error:[/red] {e}")
        return

    await _do_ingest(con, ingestor, since=None)
    db.set_last_sync(con, source, int(time.time() * 1000))
    console.print(f"[green]Done.[/green] Run `llm-archive status` to see results.")


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS)), required=False)
@click.option("--db-path", default=None, help="Override database path")
def sync(source: str | None, db_path: str | None):
    """Incremental sync — only conversations updated since last sync."""
    _run(_sync(source, db_path))


async def _sync(source: str | None, db_path_str: str | None):
    con = db.connect(Path(db_path_str) if db_path_str else db.DB_PATH)
    sources = [source] if source else list(INGESTORS)

    for src in sources:
        since = db.get_last_sync(con, src)
        ingestor = get_ingestor(src)
        console.print(f"[bold]Syncing:[/bold] {src}")
        try:
            await _do_ingest(con, ingestor, since=since)
            db.set_last_sync(con, src, int(time.time() * 1000))
        except Exception as e:
            console.print(f"[red]Error syncing {src}:[/red] {e}")


async def _do_ingest(con, ingestor, since: int | None):
    written = 0
    skipped = 0
    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"  {ingestor.source_id}", total=None)
        try:
            async for thread in ingestor.threads(since=since):
                saved = db.save_thread(con, thread)
                if saved:
                    written += 1
                else:
                    skipped += 1
                progress.update(task, description=f"  {ingestor.source_id} — {written} new, {skipped} skipped")
        except NotImplementedError as e:
            console.print(f"  [yellow]Not implemented:[/yellow] {e}")
            return
        except RuntimeError as e:
            console.print(f"  [yellow]Warning:[/yellow] {e}")
            return
        except Exception as e:
            console.print(f"  [red]Error:[/red] {e}")
            errors += 1

    status = f"[green]{written} new[/green], {skipped} skipped"
    if errors:
        status += f", [red]{errors} errors[/red]"
    console.print(f"  {ingestor.source_id}: {status}")


@main.command()
@click.option("--db-path", default=None, help="Override database path")
def status(db_path: str | None):
    """Show per-source stats: threads, messages, last sync."""
    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    stats = db.source_stats(con)

    if not stats:
        console.print("No sources initialized yet. Run `llm-archive init <source>`.")
        return

    table = Table(title="llm-archive status")
    table.add_column("Source", style="bold")
    table.add_column("Threads", justify="right")
    table.add_column("Messages", justify="right")
    table.add_column("Last sync")

    for row in stats:
        last = row["last_sync"]
        last_str = _fmt_ts(last) if last else "[dim]never[/dim]"
        table.add_row(row["id"], str(row["thread_count"]), str(row["message_count"]), last_str)

    console.print(table)


@main.command()
def sources():
    """List all available sources and their initialization status."""
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
        "claude_code": "~/.claude/projects/**/*.jsonl",
        "opencode": "~/.local/share/opencode/opencode.db",
        "windsurf": "~/.codeium/windsurf/cascade/ (encrypted, WIP)",
        "claude": "claude.ai REST API (requires Playwright login)",
    }

    for src in INGESTORS:
        status_str = "[green]initialized[/green]" if src in initialized else "[dim]not initialized[/dim]"
        table.add_row(src, status_str, notes.get(src, ""))

    console.print(table)


def _fmt_ts(ms: int) -> str:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")
