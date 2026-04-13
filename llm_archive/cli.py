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
from llm_archive.ingestors import INGESTORS, get_ingestor
from llm_archive.logging import set_console, set_verbose

console = Console()


def _run(coro):
    return asyncio.run(coro)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
def main(verbose: bool):
    """llm-archive — dump and sync AI conversations into a local SQLite database."""
    set_console(console)
    set_verbose(verbose)


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS)), required=False)
@click.option("--path", default=None, help="Override local path (for windsurf, etc.)")
@click.option("--db-path", default=None, help="Override database path")
@click.option("-f", "--force", is_flag=True, help="Force full resync (ignore last sync timestamp)")
def sync(source: str | None, path: str | None, db_path: str | None, force: bool):
    """Sync sources. Performs first-time setup automatically when needed."""
    _run(_sync(source, db_path, path, force))


async def _sync(source: str | None, db_path_str: str | None, path: str | None = None, force: bool = False):
    con = db.connect(Path(db_path_str) if db_path_str else db.DB_PATH)
    sources = [source] if source else list(INGESTORS)

    for src in sources:
        since = None if force else db.get_last_sync(con, src)
        if since is not None and _source_thread_count(con, src) == 0:
            since = None
        ingestor = get_ingestor(src)
        if path and hasattr(ingestor, "path") and src == source:
            ingestor.path = Path(path)
        db.upsert_source(con, src, {"path": path} if path and src == source else {})
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


def _source_thread_count(con, source: str) -> int:
    row = con.execute("SELECT COUNT(*) FROM threads WHERE source_id=?", (source,)).fetchone()
    return row[0] if row else 0


async def _do_ingest(con, ingestor, since: int | None, force: bool = False):
    written = 0
    skipped = 0
    errors = 0
    total = await _ingest_total(ingestor, None)  # Get total count without since filter

    # Fetch existing thread IDs and updated_at for smart sync (disabled when force=True)
    existing_threads = {}
    if not force:
        rows = con.execute("SELECT id, updated_at FROM threads WHERE source_id=?", (ingestor.source_id,)).fetchall()
        existing_threads = {row["id"]: row["updated_at"] for row in rows}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"  {ingestor.source_id}", total=total)
        try:
            # Check if ingestor supports existing_thread_ids parameter
            import inspect
            sig = inspect.signature(ingestor.threads)
            if "existing_thread_ids" in sig.parameters:
                async for thread in ingestor.threads(since=None, existing_thread_ids=existing_threads):
                    # Force update if thread exists and has newer updated_at
                    thread_force = force
                    if not force and thread.id in existing_threads:
                        db_updated_at = existing_threads[thread.id]
                        if thread.updated_at and thread.updated_at > db_updated_at:
                            thread_force = True
                    saved = db.save_thread(con, thread, force=thread_force)
                    if saved:
                        written += 1
                    else:
                        skipped += 1
                    progress.update(
                        task,
                        advance=1 if total is not None else 0,
                        description=f"  {ingestor.source_id} — {written} new, {skipped} skipped",
                    )
            else:
                async for thread in ingestor.threads(since=None):
                    saved = db.save_thread(con, thread, force=force)
                    if saved:
                        written += 1
                    else:
                        skipped += 1
                    progress.update(
                        task,
                        advance=1 if total is not None else 0,
                        description=f"  {ingestor.source_id} — {written} new, {skipped} skipped",
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

    # Calculate skipped from total - written - errors (for smart sync)
    if total is not None:
        skipped = total - written - errors

    status = f"[green]{written} new[/green], {skipped} skipped"
    if errors:
        status += f", [red]{errors} errors[/red]"
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
@click.option("--db-path", default=None, help="Override database path")
def status(db_path: str | None):
    """Show per-source stats: threads, messages, last sync."""
    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    stats = db.source_stats(con)

    if not stats:
        console.print("No sources synced yet. Run `llm-archive sync <source>`.")
        return

    table = Table(title="llm-archive status")
    table.add_column("Source", style="bold")
    table.add_column("Host")
    table.add_column("Threads", justify="right")
    table.add_column("Messages", justify="right")
    table.add_column("Last sync")

    for row in stats:
        last = row["last_sync"]
        last_str = _fmt_ts(last) if last else "[dim]never[/dim]"
        host = row["hostname"] or "[dim]—[/dim]"
        table.add_row(row["id"], host, str(row["thread_count"]), str(row["message_count"]), last_str)

    console.print(table)


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
@click.option("--limit", default=50, show_default=True, help="Maximum matches to show")
@click.option("-t", "threads_only", is_flag=True, help="Only show matching threads and match counts")
def search(phrase: str, db_path: str | None, limit: int, threads_only: bool):
    """Search all indexed messages across providers."""
    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    rows = db.search_threads(con, phrase, limit=limit) if threads_only else db.search_messages(con, phrase, limit=limit)
    if not rows:
        console.print("No matches.")
        return
    lines: list[Text | str] = []
    if threads_only:
        for i, row in enumerate(rows):
            if i:
                lines.append("")
            title = row["title"] or "untitled"
            lines.append(_header(row["source_id"], row["thread_id"], title))
            lines.append(f"  {row['match_count']} matching messages")
        _print_lines(lines)
        return
    last = None
    for row in rows:
        title = row["title"] or "untitled"
        key = (row["source_id"], row["thread_id"], title)
        if key != last:
            if last is not None:
                lines.append("")
            lines.append(_header(row["source_id"], row["thread_id"], title))
            last = key
        if row["created_at"]:
            lines.append(_msg_marker_text(row["created_at"], row["role"]))
        lines.append(_highlight_text(_snippet(row["content_clean"], phrase), phrase))
        lines.append("")
    _print_lines(lines)


@main.command()
@click.argument("thread")
@click.option("--db-path", default=None, help="Override database path")
def show(thread: str, db_path: str | None):
    """Show a full conversation by provider:id."""
    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    row = db.get_thread(con, thread)
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


def _fmt_ts(ms: int) -> str:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


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
    text = escape(text)
    parts = [re.escape(part) for part in re.findall(r"\S+", phrase) if part]
    if not parts:
        return text
    return re.sub(
        f"({'|'.join(parts)})",
        r"[bold red]\1[/]",
        text,
        flags=re.IGNORECASE,
    )


def _highlight_text(text: str, phrase: str) -> Text:
    return Text.from_markup(_highlight(text, phrase))


def _truncate(text: str, limit: int = 200) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _snippet(text: str, phrase: str, limit: int = 200) -> str:
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
