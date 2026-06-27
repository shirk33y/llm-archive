from __future__ import annotations
import zlib
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Input, Static, ListView, ListItem, Label
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
    return text[:limit - 1] + "…"





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
    
    def __init__(self, rowid: int, source: str, title: str, updated_at: int, expanded: bool = False, match_count: int = 0):
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
        role_style = {"user": "blue", "assistant": "green", "tool": "dark_orange"}.get(self.role, "grey")
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

_SUMMARY_SIZES = ["full", "tiny", "small", "medium", "large"]

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


class ShowScreen(Screen):
    """Thread viewer — delegates to less/bat for scrolling, search, markdown."""

    BINDINGS = [
        Binding("l", "open_pager", "View", priority=True),
        Binding("s", "cycle_summary", "Summary", priority=True),
        Binding("v", "toggle_verbose", "Verbose", priority=True),
        Binding("enter", "resume_session", "Resume", priority=True),
        Binding("q", "app.pop_screen", "Back", priority=True),
        Binding("escape", "app.pop_screen", "Back", priority=True),
        Binding("h", "app.pop_screen", "Back", priority=True),
    ]

    def __init__(self, thread_data: dict, con=None):
        super().__init__()
        self.thread_data = thread_data
        self.con = con
        self._summary_idx = 0
        self._verbose = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._build_header(), id="ss-header")
            yield Static(self._build_hint(), id="ss-hint")

    def on_mount(self):
        self._open_pager()

    def _build_header(self) -> Text:
        t = self.thread_data["thread"]
        title = t.get("title") or "untitled"
        source = t.get("source_id", "?")
        ts = t.get("updated_at") or t.get("created_at", 0)
        msgs = len(self.thread_data["messages"])
        line = Text()
        line.append(f" {source} ", style=f"bold {_source_color(source)}")
        line.append(_truncate(title, 80), style="bold white")
        if ts:
            line.append(f"  {_relative_time(ts)}", style="dim yellow")
        line.append(f"  {msgs} msgs", style="dim grey50")
        size = _SUMMARY_SIZES[self._summary_idx]
        if size != "full":
            line.append(f"  ⟦{size}⟧", style="dim magenta")
        return line

    def _build_hint(self) -> Text:
        return Text(
            "\n\n  l:view  v:verbose  s:summary  enter:resume  q:back",
            style="dim",
        )

    def _render_content(self, width: int = 80) -> str:
        """Render thread to ANSI-styled text for less -R or bat."""
        from io import StringIO
        from rich.console import Console
        from rich.markdown import Markdown

        buf = StringIO()
        console = Console(
            file=buf, width=width,
            force_terminal=True, color_system="256",
        )

        t = self.thread_data["thread"]
        source = t.get("source_id", "?")
        title = t.get("title") or "untitled"
        ts = t.get("updated_at") or t.get("created_at", 0)

        hdr = Text()
        hdr.append(source, style=f"bold {_source_color(source)}")
        hdr.append(f"  {title}  ", style="bold white")
        hdr.append(_relative_time(ts), style="dim yellow")
        if self._verbose:
            hdr.append("  +verbose", style="dim cyan")
        console.print(hdr)
        console.print()

        size = _SUMMARY_SIZES[self._summary_idx]
        if size != "full":
            summary = self._get_summary(size)
            if summary:
                console.print(Markdown(summary))
                console.print()
                console.print(
                    Text(f"⟦{size} · s:cycle · l:full · enter:resume · q:back⟧", style="dim")
                )
                return buf.getvalue()

        messages = self.thread_data["messages"]
        last_role: str | None = None
        for msg in messages:
            role = msg.get("role", "unknown")

            for part in msg.get("parts", []):
                if not part.get("visible"):
                    continue
                kind = part.get("kind", "text")
                text = part.get("text", "")
                if not text:
                    continue

                if kind == "text":
                    if role != last_role:
                        console.print(Text(f"── {role} ──", style=_ROLE_STYLES.get(role, "grey50")))
                        last_role = role
                    console.print(Markdown(text))

                elif self._verbose:
                    if role != last_role:
                        console.print(Text(f"── {role} ──", style=_ROLE_STYLES.get(role, "grey50")))
                        last_role = role
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

        console.print(
            Text(f"\n{len(messages)} messages · v:verbose · s:summary · enter:resume · q:back", style="dim")
        )
        return buf.getvalue()

    def _get_summary(self, size: str) -> str | None:
        if not self.con:
            return None
        row = db.get_thread_summary(self.con, self.thread_data["thread"]["id"])
        if not row:
            return None
        return row.get(size)

    def _open_pager(self):
        import os
        import shutil
        import subprocess
        import tempfile

        try:
            width = max(self.app.console.size.width - 2, 40)
        except Exception:
            width = 80

        content = self._render_content(width)
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

            with self.app.suspend():
                subprocess.run(cmd)
        except Exception:
            pass
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def action_open_pager(self):
        self._open_pager()

    def action_toggle_verbose(self):
        self._verbose = not self._verbose
        self.query_one("#ss-header", Static).update(self._build_header())
        self._open_pager()

    def action_cycle_summary(self):
        for _ in range(len(_SUMMARY_SIZES)):
            self._summary_idx = (self._summary_idx + 1) % len(_SUMMARY_SIZES)
            size = _SUMMARY_SIZES[self._summary_idx]
            if size == "full" or self._get_summary(size):
                break
        self.query_one("#ss-header", Static).update(self._build_header())
        self._open_pager()

    def action_resume_session(self):
        t = self.thread_data["thread"]
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


class ListScreen(Screen):
    """Main list screen with search/filter."""
    
    BINDINGS = [
        Binding("q", "app.quit", "Quit", priority=True),
        Binding("slash", "focus_search", "Search", priority=True),
        Binding("tab", "toggle_mode", "Toggle mode", priority=True),
        Binding("j", "cursor_down", "Down", priority=True),
        Binding("k", "cursor_up", "Up", priority=True),
        Binding("l", "select", "Open", priority=True),
        Binding("escape", "clear_search", "Clear", priority=True),
    ]
    
    show_deep = reactive(False)
    search_query = reactive("")
    
    def __init__(self, con):
        super().__init__()
        self.con = con
        self.all_threads: list[ThreadRow] = []
        self.displayed_rows: list = []  # ThreadRow or MessageRow
    
    def _load_threads(self):
        rows = db.list_threads(self.con, limit=500)
        self.all_threads = [
            ThreadRow.from_db_row(r) for r in rows if r["source_id"] != "dummy"
        ]
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
                            thread_rowid=t.rowid
                        )
                        self.displayed_rows.append(m)
            else:
                # No query in deep mode - just show all threads
                for t in self.all_threads:
                    self.displayed_rows.append(t)
        
        self._refresh_list()
    
    def _refresh_list(self):
        listview = self.query_one(ListView)
        listview.clear()
        width = max(self.size.width - 2, 20) if self.size else 80
        
        for row in self.displayed_rows:
            text = row.render(width, selected=False)
            listview.append(ListItem(Label(text)))
        
        if listview.index is None and self.displayed_rows:
            listview.index = 0
    
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("llm-archive — /:search Tab:mode j/k:nav l/Enter:open q:quit", classes="help")
            listview = ListView()
            listview.styles.height = "1fr"
            yield listview
            self.search_input = Input(placeholder="filter...", id="search")
            self.search_input.styles.display = "none"
            yield self.search_input
            status = Static("title filter mode", id="status")
            yield status
    
    def on_mount(self):
        self._load_threads()
    
    def action_focus_search(self):
        self.search_input.styles.display = "block"
        self.search_input.focus()

    def action_clear_search(self):
        self.search_input.value = ""
        self.search_input.styles.display = "none"
        self.search_query = ""
        self._update_display()
        self.set_focus(self.query_one(ListView))

    def action_toggle_mode(self):
        self.show_deep = not self.show_deep
        status = "deep search mode" if self.show_deep else "title filter mode"
        self.query_one("#status", Static).update(status)
        self._update_display()

    def action_cursor_down(self):
        lv = self.query_one(ListView)
        if lv.index is None:
            lv.index = 0
        elif lv.index < len(self.displayed_rows) - 1:
            lv.index += 1

    def action_cursor_up(self):
        lv = self.query_one(ListView)
        if lv.index is None:
            return
        if lv.index > 0:
            lv.index -= 1

    def _current_row(self):
        lv = self.query_one(ListView)
        idx = lv.index or 0
        if idx >= len(self.displayed_rows):
            return None
        return self.displayed_rows[idx]

    def action_select(self):
        row = self._current_row()
        if row is None:
            return
        if isinstance(row, ThreadRow):
            self._show_thread(row.short_id)
        else:
            thread_id = f"t{to_base53(row.thread_rowid)}"
            self._show_thread(thread_id)

    def on_list_view_selected(self, event: ListView.Selected):
        self.action_select()

    def _show_thread(self, short_id: str):
        data = db.resolve_short_id(self.con, short_id)
        if not data:
            data = db.get_thread(self.con, short_id)
        if data:
            self.app.push_screen(ShowScreen(data, self.con))

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search":
            self.search_query = event.value
            self._update_display()

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "search":
            self.search_input.styles.display = "none"
            self.set_focus(self.query_one(ListView))


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
    ListView {
        border: none;
        padding: 0;
    }
    ListView > ListItem {
        height: 1;
        padding: 0;
        border: none;
    }
    ListView > ListItem.--highlight {
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
    #ss-header {
        height: 1;
        background: $surface-darken-1;
        padding: 0 1;
    }
    #ss-hint {
        padding: 1 1;
        color: $text-muted;
    }
    """
    
    def __init__(self, db_path: Path | None = None):
        super().__init__()
        self.db_path = db_path or db.DB_PATH
        self.con = None
    
    def on_mount(self):
        self.con = db.connect(self.db_path)
        self.push_screen(ListScreen(self.con))


def run(db_path: Path | None = None):
    app = ArchiveApp(db_path)
    app.run()
