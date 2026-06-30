from __future__ import annotations
import zlib
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Input, OptionList, Static
from textual.reactive import reactive
from rich.text import Text

from llm_archive import db
from llm_archive.ids import to_base53


def _relative_time(ms: int) -> str:
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc).timestamp() * 1000
    delta_ms = now - ms
    if delta_ms < 0:
        delta_ms = 0
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


def _local_id(thread_id: str) -> str:
    if ":" not in thread_id:
        return thread_id
    return thread_id.split(":", 1)[1]


def _truncate(text: str, limit: int = 80) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


_SOURCE_COLORS: dict[str, str] = {
    "claude": "#D97757",
    "claudecode": "#D97757",
    "chatgpt": "#10A37F",
    "codex": "#40C9A2",
    "deepseek": "#4D6BFE",
    "gemini": "#4992EA",
    "cursor": "#E5C07B",
    "windsurf": "#67EADA",
    "opencode": "#22D3EE",
}

_SOURCE_PALETTE = [
    "cyan",
    "green",
    "yellow",
    "magenta",
    "blue",
    "red",
    "dark_orange",
    "deep_sky_blue",
    "spring_green2",
    "gold1",
    "medium_purple",
    "hot_pink",
]


def _source_color(source: str) -> str:
    color = _SOURCE_COLORS.get(source)
    if color:
        return color
    return _SOURCE_PALETTE[zlib.crc32(source.encode()) % len(_SOURCE_PALETTE)]


class ThreadRow:
    """Represents a thread or message row in the list."""

    def __init__(
        self,
        rowid: int,
        source: str,
        title: str,
        updated_at: int,
        expanded: bool = False,
        match_count: int = 0,
    ):
        self.rowid = rowid
        self.short_id = f"t{to_base53(rowid)}"
        self.source = source
        self.title = title or "untitled"
        self.updated_at = updated_at
        self.expanded = expanded
        self.match_count = match_count
        self.messages: list[dict] = []  # For deep search: matching messages
        self._row_type = "thread"

    @classmethod
    def from_db_row(cls, row: dict) -> "ThreadRow":
        return cls(
            rowid=row["thread_rowid"],
            source=row["source_id"],
            title=row["title"],
            updated_at=row.get("updated_at") or row.get("last_match_at", 0),
        )

    @property
    def is_thread(self) -> bool:
        return self._row_type == "thread"

    def render(self, width: int, selected: bool = False) -> Text:
        time = _relative_time(self.updated_at)
        prefix = "▶ " if not self.expanded else "▼ "
        src_len = len(self.source)
        text = _truncate(self.title, width - src_len - 9)

        line = Text()
        line.append(prefix, style="dim")
        line.append(f"{time:>3} ", style="dim yellow")
        line.append(f"{self.source} ", style=_source_color(self.source))
        line.append(text, style="white")
        return line


class MessageRow:
    """Represents a message row under an expanded thread."""

    def __init__(self, rowid: int, role: str, snippet: str, created_at: int, thread_rowid: int):
        self.rowid = rowid
        self.short_id = f"m{to_base53(rowid)}"
        self.role = role
        self.snippet = snippet
        self.created_at = created_at
        self.thread_rowid = thread_rowid

    def render(self, width: int, selected: bool = False) -> Text:
        time = _relative_time(self.created_at)
        role_style = {"user": "blue", "assistant": "green", "tool": "dark_orange"}.get(
            self.role, "grey"
        )
        text = _truncate(self.snippet, width - 20)

        line = Text()
        if selected:
            line.append("▸   ", style="bold yellow")
        else:
            line.append("    ")
        line.append(f"{self.short_id:<4} ", style="grey37")
        line.append(f"{time:>3} ", style="dim yellow")
        line.append(f"{self.role:<10} ", style=role_style)
        line.append(text, style="grey70" if not selected else "white")
        return line


_ROLE_STYLES: dict[str, str] = {
    "user": "bold cyan",
    "assistant": "bold green",
    "tool": "dark_orange",
    "system": "grey50",
}

_SEP_LINE_STYLE = "dim hot_pink"
_SEP_NUM_STYLE = "bold light_goldenrod1"
_SEP_TIME_STYLE = "hot_pink"


def _role_separator(role: str, msg_num: int, rel_time: str, width: int) -> "Text":
    """Full-width role separator: ── 1 ────...── user ────...── 9m ──"""
    num_str = str(msg_num)
    overhead = 10  # "── " + " " + " " + " " + " " + " ──"
    fixed = overhead + len(num_str) + len(role) + len(rel_time)
    total_fill = max(width - fixed, 0)
    fill1 = total_fill // 2
    fill2 = total_fill - fill1

    line = Text()
    line.append("── ", style=_SEP_LINE_STYLE)
    line.append(num_str, style=_SEP_NUM_STYLE)
    line.append(" " + "─" * fill1 + " ", style=_SEP_LINE_STYLE)
    line.append(role, style=_ROLE_STYLES.get(role, "grey50"))
    line.append(" " + "─" * fill2 + " ", style=_SEP_LINE_STYLE)
    line.append(rel_time, style=_SEP_TIME_STYLE)
    line.append(" ──", style=_SEP_LINE_STYLE)
    return line


_SUMMARY_SIZES = ["full", "tiny", "small", "medium", "large"]


def _make_markdown():
    from rich.markdown import Markdown
    from markdown_it import MarkdownIt

    parser = MarkdownIt().enable("strikethrough").enable("table")

    def _fast_md(markup: str) -> Markdown:
        md = Markdown.__new__(Markdown)
        md.markup = markup
        md.parsed = parser.parse(markup)
        md.code_theme = "monokai"
        md.justify = None
        md.style = "none"
        md.hyperlinks = True
        md.inline_code_lexer = None
        md.inline_code_theme = "monokai"
        return md

    return _fast_md


_RESUME_URLS = {
    "chatgpt": "https://chatgpt.com/c/{id}",
    "claude": "https://claude.ai/chat/{id}",
    "deepseek": "https://chat.deepseek.com/a/chat/s/{id}",
}
_RESUME_COMMANDS = {
    "claudecode": "claude --resume {id}",
    "codex": "codex resume {id}",
    "opencode": "opencode --session {id}",
}
_RESUME_UNSUPPORTED = {"cursor", "windsurf", "gemini"}


def _thread_summary(con, thread_data: dict, size: str) -> str | None:
    if not con:
        return None
    row = db.get_thread_summary(con, thread_data["thread"]["id"])
    if not row:
        return None
    return row.get(size)


def _render_thread_content(
    thread_data: dict,
    con=None,
    *,
    width: int = 80,
    summary_idx: int = 0,
    verbose: bool = False,
) -> str:
    from io import StringIO
    from rich.console import Console

    fast_md = _make_markdown()

    buf = StringIO()
    console = Console(
        file=buf,
        width=width,
        force_terminal=True,
        color_system="256",
    )

    t = thread_data["thread"]
    source = t.get("source_id", "?")
    title = t.get("title") or "untitled"
    ts = t.get("updated_at") or t.get("created_at", 0)

    hdr = Text()
    hdr.append(source, style=f"bold {_source_color(source)}")
    hdr.append(f"  {title}  ", style="bold white")
    hdr.append(_relative_time(ts), style="dim yellow")
    if verbose:
        hdr.append("  +verbose", style="dim cyan")
    console.print(hdr)
    console.print()

    size = _SUMMARY_SIZES[summary_idx]
    if size != "full":
        summary = _thread_summary(con, thread_data, size)
        if summary:
            console.print(fast_md(summary))
            console.print()
            console.print(Text(f"⟦{size} · s:cycle · l:full · enter:resume · q:back⟧", style="dim"))
            return buf.getvalue()

    messages = thread_data["messages"]
    last_role: str | None = None
    for msg_num, msg in enumerate(messages, 1):
        role = msg.get("role", "unknown")

        text_batch: list[str] = []
        for part in msg.get("parts", []):
            if not part.get("visible"):
                continue
            kind = part.get("kind", "text")
            text = part.get("text", "")
            if not text:
                continue

            if kind == "text":
                text_batch.append(text)
                continue

            if text_batch:
                if role != last_role:
                    rel = _relative_time(msg.get("created_at", 0))
                    console.print(_role_separator(role, msg_num, rel, width))
                    last_role = role
                console.print(fast_md("\n\n".join(text_batch)))
                text_batch = []

            if role != last_role:
                rel = _relative_time(msg.get("created_at", 0))
                console.print(_role_separator(role, msg_num, rel, width))
                last_role = role

            if verbose:
                if kind == "tool_call":
                    tool = part.get("tool_name", "?")
                    console.print(Text(f"  ▸ {tool}: {text[:200]}", style="dim cyan"))
                elif kind == "tool_result":
                    is_err = part.get("tool_is_error", 0)
                    preview = text[:200] + ("…" if len(text) > 200 else "")
                    console.print(Text(f"  ◀ {preview}", style="dim red" if is_err else "dim"))
                elif kind == "reasoning":
                    console.print(Text(f"  ℹ {text[:300]}", style="dim italic"))
                else:
                    console.print(Text(f"  [{kind}] {text[:150]}", style="dim grey37"))

        if text_batch:
            if role != last_role:
                rel = _relative_time(msg.get("created_at", 0))
                console.print(_role_separator(role, msg_num, rel, width))
                last_role = role
            console.print(fast_md("\n\n".join(text_batch)))

    console.print(
        Text(
            f"\n{len(messages)} messages · v:verbose · s:summary · enter:resume · q:back",
            style="dim",
        )
    )
    return buf.getvalue()


def _show_opening_screen(title: str, width: int) -> None:
    import shutil

    size = shutil.get_terminal_size((width, 24))
    message = f'Opening "{_truncate(title or "untitled", max(size.columns - 12, 20))}"'
    row = max(size.lines // 2, 1)
    col = max((size.columns - len(message)) // 2 + 1, 1)
    print("\x1b[40m\x1b[37m\x1b[2J\x1b[H", end="")
    print(f"\x1b[{row};{col}H{message}", end="", flush=True)


def _ensure_thread_stub(md_path, thread_id: str, source_id: str, title: str) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    if md_path.exists():
        return
    md_path.write_text(
        f"<!-- thread:{thread_id} source:{source_id} -->\n"
        f"# {title or 'untitled'}\n\n"
    )


def _open_thread_pager(
    app: App,
    thread_data: dict,
    con=None,
    *,
    summary_idx: int = 0,
    verbose: bool = False,
) -> None:
    import os
    import shutil
    import subprocess
    import tempfile

    try:
        width = max(app.console.size.width - 2, 40)
    except Exception:
        width = 80

    from llm_archive import glow

    if glow.is_available():
        from llm_archive import export
        from llm_archive.config import load_config

        t = thread_data["thread"]
        thread_id = t.get("id", "")
        source_id = t.get("source_id", "unknown")
        title = t.get("title", "untitled")
        config = load_config()

        md_path = export.thread_md_path(source_id, thread_id, config)
        if not md_path.exists():
            _ensure_thread_stub(md_path, thread_id, source_id, title)

        try:
            rc = None
            with app.suspend():
                _show_opening_screen(title, width)
                if md_path.exists() and glow.is_too_large(md_path):
                    pager = shutil.which("less")
                    if pager:
                        subprocess.run([pager, "-R", str(md_path)])
                        return
                if md_path.exists():
                    rc = glow.view(md_path, width=width)
                if rc == 0:
                    return
        except Exception:
            pass

    content = _render_thread_content(
        thread_data,
        con,
        width=width,
        summary_idx=summary_idx,
        verbose=verbose,
    )
    fd, path = tempfile.mkstemp(suffix=".md", prefix="llm-archive-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)

        bat = shutil.which("bat") or shutil.which("batcat")
        if bat:
            cmd = [bat, "--paging=always", "--style=plain", "--color=always", path]
        elif shutil.which("less"):
            cmd = ["less", "-R", path]
        else:
            print(content)
            return

        with app.suspend():
            subprocess.run(cmd)
    except Exception:
        pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class ListScreen(Screen):
    """Main list screen with search/filter."""

    BINDINGS = [
        Binding("q", "app.quit", "Quit", priority=True),
        Binding("slash", "focus_search", "Search", priority=True),
        Binding("tab", "toggle_mode", "Toggle mode", priority=True),
        Binding("j", "cursor_down", "Down", priority=True),
        Binding("k", "cursor_up", "Up", priority=True),
        Binding("l", "select", "Open", priority=True),
        Binding("enter", "resume_session", "Resume", priority=True),
        Binding("escape", "clear_search", "Clear", priority=True),
    ]

    show_deep = reactive(False)
    search_query = reactive("")

    def __init__(self, con):
        super().__init__()
        self.con = con
        self.all_threads: list[ThreadRow] = []
        self.displayed_rows: list = []  # ThreadRow or MessageRow
        self.status_warning = ""

    def _mode_status(self) -> str:
        return "deep search mode" if self.show_deep else "title filter mode"

    def _status_text(self) -> str:
        if self.status_warning:
            return f"{self._mode_status()} | {self.status_warning}"
        return self._mode_status()

    def _load_status_warning(self) -> None:
        alerts = db.database_write_alerts(db.provider_states(self.con))
        if not alerts:
            self.status_warning = ""
            return
        sources = ", ".join(alert["source_id"] for alert in alerts)
        if any(alert["kind"] == "storage_full" for alert in alerts):
            self.status_warning = f"storage full: {sources}"
        else:
            self.status_warning = f"database write failed: {sources}"

    def _load_threads(self):
        rows = db.list_threads(self.con, limit=500)
        self.all_threads = [ThreadRow.from_db_row(r) for r in rows if r["source_id"] != "dummy"]
        self._update_display()

    def _update_display(self):
        """Update displayed rows based on search mode and query."""
        self.displayed_rows = []
        query = self.search_query.lower()

        if not self.show_deep:
            # Title filter mode
            for t in self.all_threads:
                if not query or query in t.title.lower():
                    self.displayed_rows.append(t)
        else:
            # Deep search mode - needs FTS query
            if query:
                # Search for matching threads and messages
                results = db.search_threads(self.con, query, limit=100)
                for r in results:
                    t = ThreadRow.from_db_row(r)
                    t.match_count = r.get("match_count", 0)
                    t.expanded = True  # Auto-expand threads with matches
                    self.displayed_rows.append(t)

                    # Add matching messages as children
                    msg_rows = db.search_messages(self.con, query, limit=t.match_count)
                    for mr in msg_rows:
                        if mr["thread_id"] != t.rowid:
                            continue
                        snippet = mr.get("content_clean", "")[:100]
                        m = MessageRow(
                            rowid=mr["message_rowid"],
                            role=mr["role"],
                            snippet=snippet,
                            created_at=mr["created_at"],
                            thread_rowid=t.rowid,
                        )
                        self.displayed_rows.append(m)
            else:
                # No query in deep mode - just show all threads
                for t in self.all_threads:
                    self.displayed_rows.append(t)

        self._refresh_list()

    def _refresh_list(self):
        option_list = self.query_one(OptionList)
        width = max(self.size.width - 2, 20) if self.size else 80
        option_list.set_options(row.render(width, selected=False) for row in self.displayed_rows)
        if option_list.highlighted is None and self.displayed_rows:
            option_list.highlighted = 0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                "llm-archive — /:search Tab:mode j/k:nav l:view Enter:resume q:quit", classes="help"
            )
            options = OptionList()
            options.styles.height = "1fr"
            yield options
            self.search_input = Input(placeholder="filter...", id="search")
            self.search_input.styles.display = "none"
            yield self.search_input
            status = Static(self._status_text(), id="status")
            yield status

    def on_mount(self):
        self._load_status_warning()
        self._load_threads()
        self.query_one("#status", Static).update(self._status_text())

    def action_focus_search(self):
        self.search_input.styles.display = "block"
        self.search_input.focus()

    def action_clear_search(self):
        self.search_input.value = ""
        self.search_input.styles.display = "none"
        self.search_query = ""
        self._update_display()
        self.set_focus(self.query_one(OptionList))

    def action_toggle_mode(self):
        self.show_deep = not self.show_deep
        self.query_one("#status", Static).update(self._status_text())
        self._update_display()

    def action_cursor_down(self):
        option_list = self.query_one(OptionList)
        if option_list.highlighted is None:
            option_list.highlighted = 0
        elif option_list.highlighted < len(self.displayed_rows) - 1:
            option_list.highlighted += 1

    def action_cursor_up(self):
        option_list = self.query_one(OptionList)
        if option_list.highlighted is None:
            return
        if option_list.highlighted > 0:
            option_list.highlighted -= 1

    def _current_row(self):
        option_list = self.query_one(OptionList)
        idx = option_list.highlighted or 0
        if idx >= len(self.displayed_rows):
            return None
        return self.displayed_rows[idx]

    def _current_short_id(self) -> str | None:
        row = self._current_row()
        if row is None:
            return None
        if isinstance(row, ThreadRow):
            return row.short_id
        return f"t{to_base53(row.thread_rowid)}"

    def _resolve_thread(self, short_id: str) -> dict | None:
        return db.resolve_short_id(self.con, short_id) or db.get_thread(self.con, short_id)

    def action_select(self):
        short_id = self._current_short_id()
        if short_id:
            self._show_thread(short_id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.action_select()

    def _show_thread(self, short_id: str):
        data = self._resolve_thread(short_id)
        if data:
            _open_thread_pager(self.app, data, self.con)

    def action_resume_session(self):
        short_id = self._current_short_id()
        if short_id is None:
            return
        data = self._resolve_thread(short_id)
        if not data:
            return
        t = data["thread"]
        source = t.get("source_id", "")
        local_id = _local_id(t["id"])
        if source in _RESUME_UNSUPPORTED:
            self.app.bell()
            return
        if source in _RESUME_URLS:
            import webbrowser

            webbrowser.open(_RESUME_URLS[source].format(id=local_id))
        elif source in _RESUME_COMMANDS:
            import subprocess

            subprocess.Popen(_RESUME_COMMANDS[source].format(id=local_id).split())
        else:
            self.app.bell()

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search":
            self.search_query = event.value
            self._update_display()

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "search":
            self.search_input.styles.display = "none"
            self.set_focus(self.query_one(OptionList))


class ArchiveApp(App):
    """Main TUI application."""

    CSS = """
    Screen {
        border: none;
        padding: 0;
    }
    .help {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
        text-style: dim;
    }
    .header {
        height: 2;
        background: $surface-darken-1;
        color: $text;
        text-style: bold;
    }
    OptionList {
        border: none;
        padding: 0;
    }
    OptionList > .option-list--option {
        padding: 0;
    }
    OptionList > .option-list--option-highlighted {
        background: $primary-darken-2;
    }
    Input {
        border: none;
        background: $surface-darken-1;
    }
    Static#status {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
        text-style: dim;
    }
    """

    def __init__(self, db_path: Path | None = None):
        super().__init__()
        self.db_path = db_path or db.DB_PATH
        self.con = None

    def on_mount(self):
        self.con = db.connect_readonly(self.db_path)
        self.push_screen(ListScreen(self.con))


def run(db_path: Path | None = None):
    app = ArchiveApp(db_path)
    app.run()
