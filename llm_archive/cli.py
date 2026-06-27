from __future__ import annotations
from collections.abc import Sequence
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import NamedTuple

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.markdown import Markdown
from rich.text import Text

from llm_archive import db
from llm_archive.sync import _run, _sync_command, _sync_one
from llm_archive.backup import run_backup
from llm_archive.config import config_path, format_duration_ms, load_config, read_config_text
from llm_archive.ids import to_base53
from llm_archive.ingestors import INGESTORS
from llm_archive.jobs import ensure_fresh
from llm_archive.logging import set_console as set_log_console, set_verbose
from llm_archive.providers import provider_kind
from llm_archive.setup import disable_provider, enable_provider, setup_summary
from llm_archive.service_cli import service as service_command

SERVICE_HINT_INSTALL = "llm-archive start --install"
SERVICE_HINT_START = "llm-archive start"
SERVICE_HINT_RESTART = "llm-archive restart"

console = Console()
progress_console = Console()


_COMMAND_ORDER = [
    "search", "show", "tui", "status",
    "sync", "embed", "sum",
    "enable", "disable", "config", "resume",
    "start", "stop", "restart", "logs",
    "service", "mcp", "backup",
]


class OrderedGroup(click.Group):
    def list_commands(self, ctx):
        ordered = [c for c in _COMMAND_ORDER if c in self.commands]
        remaining = sorted(c for c in self.commands if c not in _COMMAND_ORDER)
        return ordered + remaining


@click.group(cls=OrderedGroup)
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
def main(verbose: bool):
    """llm-archive — dump and sync AI conversations into a local SQLite database."""
    set_log_console(progress_console)
    set_verbose(verbose)


main.add_command(service_command, "service")


@main.command()
@click.argument("sources", nargs=-1, type=click.Choice(list(INGESTORS)), metavar="SOURCE")
@click.option("--path", default=None, help="Override local path (for windsurf, etc.)")
@click.option("--db-path", default=None, help="Override database path")
@click.option("-f", "--force", is_flag=True, help="Force full resync (ignore last sync timestamp)")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
@click.option(
    "--auth-mode",
    type=click.Choice(["cookies"]),
    default=None,
    help="Override auth mode for this sync",
)
@click.option("--no-wait", is_flag=True, help="Do not wait if source already syncing")
@click.option("--json", "json_output", is_flag=True, help="Print JSON")
def sync(
    sources: tuple[str, ...],
    path: str | None,
    db_path: str | None,
    force: bool,
    verbose: bool,
    auth_mode: str | None,
    no_wait: bool,
    json_output: bool,
):
    """Sync sources. Performs first-time setup automatically when needed."""
    if verbose:
        set_verbose(True)
    for source in sources or [None]:
        _run(_sync_command(source, db_path, path, force, auth_mode, no_wait, json_output))


@main.command()
@click.argument("sources", nargs=-1, type=click.Choice(list(INGESTORS)), metavar="SOURCE")
@click.option("--browser", default=None, help="Detected browser name")
@click.option("--profile", default=None, help="Browser profile name")
@click.option("--browser-path", default=None, help="Custom browser/profile path")
@click.option("--path", default=None, help="Custom file-provider data path")
@click.option("--force", is_flag=True, help="Reconfigure existing source")
@click.option("--dry-run", is_flag=True, help="Detect and verify only")
def enable(
    sources: tuple[str, ...],
    browser: str | None,
    profile: str | None,
    browser_path: str | None,
    path: str | None,
    force: bool,
    dry_run: bool,
):
    """Configure sources and verify auth/path."""
    for source in sources:
        _enable_one(source, browser, profile, browser_path, path, force, dry_run)


def _enable_one(
    source: str,
    browser: str | None,
    profile: str | None,
    browser_path: str | None,
    path: str | None,
    force: bool,
    dry_run: bool,
):
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
    console.print(f"Run `llm-archive sync {source}` to start sync.")


@main.command()
@click.argument("sources", nargs=-1, type=click.Choice(list(INGESTORS)), metavar="SOURCE")
@click.option("--no-confirm", is_flag=True, help="Disable without prompt")
def disable(sources: tuple[str, ...], no_confirm: bool):
    """Disable source scheduling/watchers, keep archived data."""
    if not no_confirm and not click.confirm(f"Disable {', '.join(sources)}?", default=True):
        return
    for source in sources:
        disable_provider(source)
        con = db.connect(db.DB_PATH)
        try:
            db.set_provider_enabled(con, source, False)
        finally:
            con.close()
        console.print(f"{source} disabled")


class _StatusRow(NamedTuple):
    source_id: str
    state: str
    last: str
    threads: str
    messages: str
    size: str
    next_sync: str
    stale: bool


def _human_size(n: int) -> str:
    if n == 0:
        return "-"
    for unit, threshold in [("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)]:
        if n >= threshold:
            val = n / threshold
            if val < 10:
                return f"{val:.1f}{unit}".replace(".0", "")
            return f"{val:.0f}{unit}"
    return str(n)


@main.command()
@click.option("--db-path", default=None, help="Override database path")
@click.option("--verbose", is_flag=True, help="Include setup checks and fix hints")
@click.option("--json", "json_output", is_flag=True, help="Print JSON")
def status(db_path: str | None, verbose: bool, json_output: bool):
    """Show service, provider, auth, backup, and freshness state."""
    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    try:
        stats = db.source_stats(con)
        states = db.provider_states(con)
        sizes = db.source_sizes(con)
        service_state = db.get_service_state(con)
        backup_state = db.get_backup_state(con)
        jobs = db.recent_jobs(con, 10)
    finally:
        con.close()
    config = load_config()

    if json_output:
        console.print_json(
            data={
                "service": service_state,
                "backup": backup_state,
                "providers": [item for key, item in states.items() if key != "dummy"],
                "sources": [item for item in stats if item["id"] != "dummy"],
                "jobs": jobs,
            }
        )
        return

    lines = []
    if not service_state or not service_state.get("heartbeat_at"):
        lines.append(f"SERVICE stopped  ({SERVICE_HINT_START})")
    else:
        age = int(time.time() * 1000) - int(service_state["heartbeat_at"])
        if age > 90_000:
            lines.append(
                f"SERVICE stale  pid {service_state.get('pid')}, heart {_relative_time(service_state['heartbeat_at'])} ago  ({SERVICE_HINT_RESTART})"
            )
        else:
            lines.append(
                f"SERVICE up  pid {service_state.get('pid')}, heart {format_duration_ms(age)} ago"
            )

    if not backup_state or not backup_state.get("last_success_at"):
        lines.append("BACKUP never")
    elif backup_state.get("last_error"):
        lines.append(f"BACKUP failed  {backup_state['last_error']}")
    else:
        last = _relative_time(backup_state["last_success_at"])
        next_backup_at = backup_state.get("next_backup_at")
        nxt = _until(next_backup_at) if isinstance(next_backup_at, int) else "-"
        lines.append(f"BACKUP ok  last {last}, next {nxt}")

    lines.append("")

    by_source = {row["id"]: row for row in stats}
    active_syncs = {
        item["source_id"]
        for item in jobs
        if item["kind"] == "sync" and item["status"] == "running" and item["source_id"]
    }
    rows: list[_StatusRow] = []
    for source_id in sorted((set(by_source) | set(states)) - {"dummy"}):
        row = by_source.get(source_id, {})
        state = states.get(source_id, {})
        last = row.get("last_sync")
        last_str = _relative_time(last) if last else "-"
        one_day_ms = 86_400 * 1000
        last_old = last is not None and (int(time.time() * 1000) - last) >= one_day_ms
        has_synced = bool(state.get("last_success_at")) or bool(last)
        watch_seen_at = state.get("watch_seen_at")
        watch_age = (
            int(time.time() * 1000) - int(watch_seen_at)
            if isinstance(watch_seen_at, int)
            else 91_000
        )
        watched = (
            config.ingestor(source_id).watch
            and provider_kind(source_id) == "file"
            and state.get("watch_active")
            and watch_age <= 90_000
        )
        if watched and has_synced:
            next_str = "watching"
        else:
            next_sync = state.get("next_sync_at")
            next_str = _until(next_sync) if next_sync else "-"
        thr = str(row.get("thread_count", 0))
        msg = str(row.get("message_count", 0))
        size = _human_size(sizes.get(source_id, 0))
        if source_id not in config.ingestors or not state.get("enabled"):
            st = "off"
        elif state.get("last_error"):
            st = "error"
        elif source_id in active_syncs:
            st = "sync"
        else:
            st = "ok"
        rows.append(_StatusRow(source_id, st, last_str, thr, msg, size, next_str, last_old))

    w_src = max(max((len(r.source_id) for r in rows), default=0), len("SOURCE"), 6)
    w_st = max(max((len(r.state) for r in rows), default=0), len("STATE"), 5)
    w_lst = max(max((len(r.last) for r in rows), default=0), len("LAST"), 5)
    w_thr = max(max((len(r.threads) for r in rows), default=0), len("THR"), 3)
    w_msg = max(max((len(r.messages) for r in rows), default=0), len("MSG"), 3)
    w_sz = max(max((len(r.size) for r in rows), default=0), len("SIZE"), 4)
    hdr = f"{'SOURCE':<{w_src}}  {('STATE'):<{w_st}}  {('LAST'):<{w_lst}}  {('THR'):>{w_thr}}  {('MSG'):>{w_msg}}  {'SIZE':>{w_sz}}  NEXT"
    lines.append(hdr)
    for r in rows:
        lst_cell = f"{r.last:<{w_lst}}"
        if r.stale:
            lst_cell = f"[orange3]{lst_cell}[/orange3]"
        lines.append(
            f"{r.source_id:<{w_src}}  {r.state:<{w_st}}  {lst_cell}  {r.threads:>{w_thr}}  {r.messages:>{w_msg}}  {r.size:>{w_sz}}  {r.next_sync}"
        )

    total_thr = sum(int(r.threads) for r in rows)
    total_msg = sum(int(r.messages) for r in rows)
    total_size = _human_size(sum(sizes.get(r.source_id, 0) for r in rows))
    lines.append("")
    lines.append(
        f"{'TOTAL':<{w_src}}  {'':<{w_st}}  {'':<{w_lst}}  {total_thr:>{w_thr}}  {total_msg:>{w_msg}}  {total_size:>{w_sz}}"
    )

    for line in lines:
        console.print(line)

    if verbose:
        _print_verbose_status(states, jobs)


@main.command()
@click.argument("phrase")
@click.option("--db-path", default=None, help="Override database path")
@click.option("--limit", default=200, show_default=True, help="Maximum matches to show")
@click.option("--provider", "provider_filter", type=click.Choice(list(INGESTORS)), default=None)
@click.option("--sync", "do_sync", is_flag=True, help="Trigger sync before searching")
@click.option("-s", "--semantic", is_flag=True, help="Semantic search via embeddings")
@click.option("--json", "json_output", is_flag=True, help="Print JSON")
@click.option(
    "-t", "threads_only", is_flag=True, help="Only show matching threads and match counts"
)
def search(
    phrase: str,
    db_path: str | None,
    limit: int,
    provider_filter: str | None,
    do_sync: bool,
    semantic: bool,
    json_output: bool,
    threads_only: bool,
):
    """Search all indexed messages across providers. Use --semantic for vector similarity search."""
    if do_sync:
        config = load_config()

        async def runner(src: str, job_force: bool) -> bool:
            return await _sync_one(src, db_path, None, job_force, None)

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
    try:
        if semantic:
            from llm_archive import embed as embed_mod

            model = embed_mod.DEFAULT_MODEL
            has_vec, _ = db.init_embeddings(con, embed_mod.get_dims(model))
            if not has_vec:
                console.print("[red]sqlite-vec not installed.[/red] Run: [bold]pip install sqlite-vec[/bold]")
                raise SystemExit(1)
            try:
                vector = embed_mod.embed_text(phrase, model)
            except Exception as exc:
                console.print(f"[red]Embedding failed[/red] — {exc}")
                raise SystemExit(1)
            blob = embed_mod.serialize(vector)
            rows = db.semantic_search_threads(con, blob, limit=limit, source_id=provider_filter)
            if json_output:
                console.print_json(data={"query": phrase, "results": rows, "count": len(rows)})
                return
            if not rows:
                console.print("No matches.")
                return
            formatted_rows = []
            for i, row in enumerate(rows):
                if i:
                    formatted_rows.append(None)
                title = row["title"] or "untitled"
                short_id = f"t{to_base53(row['thread_rowid'])}"
                rel_time = _relative_time(row["updated_at"])
                dist = f"{row['distance']:.3f}"
                formatted_rows.append(
                    {"id": short_id, "time": rel_time, "source": row["source_id"], "text": title, "dist": dist}
                )
            max_id_width = max((len(r["id"]) for r in formatted_rows if r is not None), default=0)
            max_time_width = max((len(r["time"]) for r in formatted_rows if r is not None), default=0)
            lines: list[Text | str | Markdown] = []
            for row in formatted_rows:
                if row is None:
                    lines.append("")
                else:
                    line = _search_thread_line(
                        row["id"], row["time"], row["source"], row["text"], max_id_width, max_time_width
                    )
                    line.append(f"  {row['dist']}", style="dim cyan")
                    lines.append(line)
            _print_lines(lines)
            return
        rows = (
            db.search_threads(con, phrase, limit=limit)
            if threads_only
            else db.search_messages(con, phrase, limit=limit)
        )
    finally:
        con.close()
    if provider_filter:
        rows = [row for row in rows if row["source_id"] == provider_filter]
    if json_output:
        console.print_json(data={"query": phrase, "results": rows, "count": len(rows)})
        return
    if not rows:
        console.print("No matches.")
        return
    lines: list[Text | str | Markdown] = []
    if threads_only:
        formatted_rows = []
        for i, row in enumerate(rows):
            if i:
                formatted_rows.append(None)
            title = row["title"] or "untitled"
            short_id = f"t{to_base53(row['thread_rowid'])}"
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
            short_id = f"t{to_base53(row['thread_rowid'])}"
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
        short_id = f"m{to_base53(row['message_rowid'])}"
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
    try:
        row = db.resolve_short_id(con, thread) or db.get_thread(con, thread)
    finally:
        con.close()

    if row is None:
        console.print(f"Thread not found: {thread}")
        raise SystemExit(1)
    info = row["thread"]
    lines: list[Text | str | Markdown] = []
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
            label = _part_label(part["kind"], data, part)
            if label:
                lines.append(f"  {label}")
            if part["text"]:
                lines.append(_render_markdown(part["text"]))
        lines.append("")
    _print_lines(lines)


RESUME_URLS = {
    "chatgpt": "https://chatgpt.com/c/{id}",
    "claude": "https://claude.ai/chat/{id}",
    "deepseek": "https://chat.deepseek.com/a/chat/s/{id}",
}

RESUME_COMMANDS = {
    "claudecode": "claude --resume {id}",
    "codex": "codex resume {id}",
    "opencode": "opencode --session {id}",
}

RESUME_UNSUPPORTED = {"cursor", "windsurf", "gemini"}


@main.command()
@click.argument("thread_id")
@click.option("--db-path", default=None, help="Override database path")
def resume(thread_id: str, db_path: str | None):
    """Resume a conversation by opening it in the provider (browser or CLI)."""
    import webbrowser

    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    try:
        row = db.resolve_short_id(con, thread_id) or db.get_thread(con, thread_id)
    finally:
        con.close()
    if not row:
        console.print(f"Thread not found: {thread_id}")
        raise SystemExit(1)

    thread = row["thread"]
    source_id = thread["source_id"]
    local_id = _local_id(thread["id"])

    if source_id in RESUME_UNSUPPORTED:
        console.print(f"{source_id} does not support resuming conversations")
        raise SystemExit(1)

    if source_id in RESUME_URLS:
        url = RESUME_URLS[source_id].format(id=local_id)
        console.print(url)
        webbrowser.open(url)
    elif source_id in RESUME_COMMANDS:
        cmd = RESUME_COMMANDS[source_id].format(id=local_id)
        console.print(cmd)
        import subprocess

        subprocess.Popen(cmd.split())
    else:
        console.print(f"No resume handler for {source_id}")
        raise SystemExit(1)


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


def _until(ms: int) -> str:
    remaining = int(ms - time.time() * 1000)
    if remaining <= 0:
        overdue = format_duration_ms(-remaining)
        return overdue
    return format_duration_ms(remaining)


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
            console.print(
                f"{source_id}: fix setup, then run `llm-archive enable {source_id} --force`"
            )


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


def _part_label(kind: str, data: dict, part: dict | None = None) -> str:
    if kind == "tool_call":
        if data.get("tag"):
            return f"[{data['tag']}]"
        name = data.get("name") or (part and part.get("tool_name"))
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


def _print_lines(lines: Sequence[Text | str | Markdown]) -> None:
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
@click.option("--db-path", default=None, help="Override database path")
def embed(
    source: str | None,
    force: bool,
    db_path: str | None,
):
    """Generate embeddings for semantic search (uses fastembed, local)."""
    import time as _time
    from llm_archive import embed as embed_mod

    model = embed_mod.DEFAULT_MODEL
    dims = embed_mod.get_dims(model)
    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    try:
        has_vec, dim_mismatch = db.init_embeddings(con, dims)
        if not has_vec:
            console.print(
                "[red]sqlite-vec not installed.[/red] Run: [bold]pip install sqlite-vec[/bold]"
            )
            return

        if dim_mismatch and not force:
            existing_count = con.execute("SELECT COUNT(*) FROM thread_embeddings").fetchone()[0]
            console.print(
                f"[yellow]Dimension mismatch:[/yellow] existing embeddings use different dimensions.\n"
                f"  {existing_count} embeddings will be lost if you rebuild.\n"
                f"  Run [bold]llm-archive embed --force[/bold] to rebuild with {dims}d vectors."
            )
            return

        if dim_mismatch and force:
            con.execute("DELETE FROM thread_embeddings")
            con.execute("DROP TABLE IF EXISTS vec_threads")
            db.init_embeddings(con, dims)

        thread_ids = embed_mod.threads_needing_embedding(con, source, force)
        if not thread_ids:
            console.print("All threads already embedded. Use [bold]--force[/bold] to re-embed.")
            return

        console.print(f"Embedding [bold]{len(thread_ids)}[/bold] threads using [cyan]{model}[/cyan]...")
        now = int(_time.time() * 1000)
        BATCH_SIZE = 256

        errors = 0
        skipped = 0
        done = 0
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=progress_console,
            transient=True,
        ) as progress:
            task = progress.add_task("Embedding", total=len(thread_ids))
            for i in range(0, len(thread_ids), BATCH_SIZE):
                batch_ids = thread_ids[i : i + BATCH_SIZE]
                texts = []
                valid_ids = []
                for tid in batch_ids:
                    try:
                        text = embed_mod.extract_thread_text(con, tid)
                        if text.strip():
                            texts.append(text)
                            valid_ids.append(tid)
                        else:
                            skipped += 1
                    except Exception as e:
                        errors += 1
                        console.print(f"  [red]Error[/red] {tid}: {e}")

                if not texts:
                    progress.advance(task, len(batch_ids))
                    continue

                try:
                    vectors = embed_mod.embed_batch(texts, model)
                    for tid, vector in zip(valid_ids, vectors):
                        blob = embed_mod.serialize(vector)
                        db.upsert_thread_embedding(con, tid, model, blob, now)
                    done += len(valid_ids)
                except Exception as e:
                    errors += len(valid_ids)
                    console.print(f"  [red]Batch error:[/red] {e}")

                progress.advance(task, len(batch_ids))

        parts = [f"[green]{done}[/green] embedded"]
        if skipped:
            parts.append(f"[dim]{skipped}[/dim] skipped (empty)")
        if errors:
            parts.append(f"[red]{errors}[/red] errors")
        console.print("  " + ", ".join(parts))
    finally:
        con.close()


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
    finally:
        con.close()
    if json_output:
        console.print_json(data={"status": "ok", "path": str(target), "verified": verify})
        return
    console.print(f"backup ok: {target}")


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
@click.option("-n", "--lines", default=100, show_default=True, type=int, help="Number of log lines")
@click.option("-f", "--follow", is_flag=True, help="Follow logs")
def logs(lines: int, follow: bool):
    """Show scheduler service process logs."""
    from llm_archive.service_control import service_logs

    service_logs(lines, follow)


@main.command()
@click.option("--install", "do_install", is_flag=True, help="Register the service first, then start")
def start(do_install: bool):
    """Start the scheduler service (register with --install)."""
    from llm_archive.service_control import is_service_installed, start_service

    if not do_install and not is_service_installed():
        console.print(
            "[yellow]service not installed.[/yellow] run: "
            f"[bold]{SERVICE_HINT_INSTALL}[/bold]"
        )
        raise SystemExit(1)
    start_service(install=do_install)
    console.print("service started")


@main.command()
@click.option("--uninstall", "do_uninstall", is_flag=True, help="Also unregister and remove the service")
def stop(do_uninstall: bool):
    """Stop the scheduler service (unregister with --uninstall)."""
    from llm_archive.service_control import stop_service

    stop_service(uninstall=do_uninstall)
    console.print("service stopped")


@main.command()
def restart():
    """Restart the scheduler service."""
    from llm_archive.service_control import restart_service

    restart_service()
    console.print("service stopped")
    console.print("service started")


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS)), required=False)
@click.option("--force", "-f", is_flag=True, help="Re-summarize all threads")
@click.option("--model", default="ollama/qwen2.5:7b", help="litellm model string (e.g. ollama/qwen2.5:7b, anthropic/claude-sonnet-4-20250514)")
@click.option("--limit", "-n", default=0, type=int, help="Max threads to summarize (0=all)")
@click.option("--db-path", default=None, help="Override database path")
def sum_cmd(
    source: str | None,
    force: bool,
    model: str,
    limit: int,
    db_path: str | None,
):
    """Summarize threads using an LLM via litellm."""
    from llm_archive import summarize as sum_mod

    con = db.connect(Path(db_path) if db_path else db.DB_PATH)
    try:
        threads = db.threads_needing_summary(con, source, force=force)

        if not threads:
            console.print("No threads need summarizing.")
            return

        if limit > 0:
            threads = threads[:limit]

        console.print(
            f"Summarizing [bold]{len(threads)}[/bold] threads using "
            f"[cyan]{model}[/cyan]..."
        )

        done = 0
        errors = 0
        t_start = time.time()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=progress_console,
            transient=True,
        ) as progress:
            task = progress.add_task("Summarizing", total=len(threads))
            for t in threads:
                tid = t["id"]
                result = sum_mod.summarize_thread(con, tid, model)
                if result:
                    db.upsert_thread_summary(
                        con,
                        tid,
                        result.get("tiny", ""),
                        result.get("small", ""),
                        result.get("medium", ""),
                        result.get("large", ""),
                        model,
                        int(time.time() * 1000),
                    )
                    done += 1
                else:
                    errors += 1
                progress.advance(task, 1)

        elapsed = time.time() - t_start
        parts = [f"[green]{done}[/green] summarized"]
        if errors:
            parts.append(f"[red]{errors}[/red] errors")
        parts.append(f"({elapsed:.0f}s, {elapsed/max(done,1):.1f}s/thread)")
        console.print("  " + ", ".join(parts))
    finally:
        con.close()


@main.command()
def mcp():
    """Start MCP server for conversation search and retrieval (stdio transport)."""
    from llm_archive.mcp_server import run_sync

    run_sync()
