from __future__ import annotations
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
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
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


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
        source = self.source[:3]
        text = _truncate(self.title, width - 15)
        
        line = Text()
        if selected:
            line.append("▸ ", style="bold yellow")
        else:
            line.append("  ")
        line.append(prefix, style="dim")
        line.append(f"{time:>3} ", style="dim yellow")
        line.append(f"{source:<3} ", style="orange1")
        line.append(text, style="bold white" if selected else "white")
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


class ShowScreen(Screen):
    """Full-screen thread view."""
    
    BINDINGS = [
        ("h,left,escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
    ]
    
    def __init__(self, thread_data: dict):
        super().__init__()
        self.thread_data = thread_data
    
    def compose(self) -> ComposeResult:
        with Vertical():
            header = self._render_header()
            yield Static(header, classes="header")
            with VerticalScroll():
                content = self._render_content()
                yield Static(content, classes="content")
    
    def _render_header(self) -> str:
        t = self.thread_data["thread"]
        title = t["title"] or "untitled"
        return f"◀ {t['source_id']} · {title}\n"
    
    def _render_content(self) -> str:
        lines = []
        for msg in self.thread_data["messages"]:
            ts = msg.get("created_at", 0)
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            role = msg.get("role", "unknown")
            lines.append(f"\n─ {time_str} · {role} ─".rjust(60, "─"))
            
            for part in msg.get("parts", []):
                if not part.get("visible"):
                    continue
                text = part.get("text", "")
                if text:
                    lines.append(text)
        return "\n".join(lines)


class ListScreen(Screen):
    """Main list screen with search/filter."""
    
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("slash", "focus_search", "Search"),
        ("tab", "toggle_mode", "Toggle mode"),
        ("j,down", "cursor_down", "Down"),
        ("k,up", "cursor_up", "Up"),
        ("l,right,enter", "select", "Select/Expand"),
        ("h,left", "collapse", "Collapse"),
        ("escape", "clear_search", "Clear"),
    ]
    
    show_deep = reactive(False)
    search_query = reactive("")
    
    def __init__(self, con):
        super().__init__()
        self.con = con
        self.all_threads: list[ThreadRow] = []
        self.displayed_rows: list = []  # ThreadRow or MessageRow
        self.cursor_idx = 0
        self._load_threads()
    
    def _load_threads(self):
        rows = db.list_threads(self.con, limit=500)
        self.all_threads = [ThreadRow.from_db_row(r) for r in rows]
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
        width = self.size.width - 2 if self.size else 80
        
        for i, row in enumerate(self.displayed_rows):
            selected = i == self.cursor_idx
            if isinstance(row, ThreadRow):
                text = row.render(width, selected)
            else:
                text = row.render(width, selected)
            listview.append(ListItem(Label(text)))
    
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
        self._refresh_list()
    
    def action_focus_search(self):
        self.search_input.styles.display = "block"
        self.search_input.focus()
    
    def action_clear_search(self):
        self.search_input.value = ""
        self.search_input.styles.display = "none"
        self.search_query = ""
        self.cursor_idx = 0
        self._update_display()
        self.set_focus(self.query_one(ListView))
    
    def action_toggle_mode(self):
        self.show_deep = not self.show_deep
        status = "deep search mode" if self.show_deep else "title filter mode"
        self.query_one("#status", Static).update(status)
        self._update_display()
    
    def action_cursor_down(self):
        if self.cursor_idx < len(self.displayed_rows) - 1:
            self.cursor_idx += 1
            self._refresh_list()
            self.query_one(ListView).index = self.cursor_idx
    
    def action_cursor_up(self):
        if self.cursor_idx > 0:
            self.cursor_idx -= 1
            self._refresh_list()
            self.query_one(ListView).index = self.cursor_idx
    
    def action_select(self):
        if not self.displayed_rows:
            return
        row = self.displayed_rows[self.cursor_idx]
        if isinstance(row, ThreadRow):
            if not self.show_deep:
                # Toggle expand/collapse in filter mode
                row.expanded = not row.expanded
                self._refresh_list()
            else:
                # In deep mode, Enter shows the thread
                self._show_thread(row.short_id)
        else:
            # Message row - show parent thread
            thread_id = f"t{to_base53(row.thread_rowid)}"
            self._show_thread(thread_id)
    
    def action_collapse(self):
        if not self.displayed_rows:
            return
        row = self.displayed_rows[self.cursor_idx]
        if isinstance(row, ThreadRow):
            row.expanded = False
            self._refresh_list()
    
    def _show_thread(self, short_id: str):
        data = db.resolve_short_id(self.con, short_id)
        if not data:
            data = db.get_thread(self.con, short_id)
        if data:
            self.app.push_screen(ShowScreen(data))
    
    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search":
            self.search_query = event.value
            self.cursor_idx = 0
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
