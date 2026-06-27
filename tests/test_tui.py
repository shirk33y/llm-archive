"""TUI tests using Textual's headless Pilot API + asciicast recording helper.

Two layers:
1. run_test() pilot tests — headless, assert widget state, keypress behavior
2. record_asciicast() — runs TUI in PTY, emits asciicast v2 JSON for upload

All tests reuse the same conftest fixtures (isolate_archive_paths, close_sqlite_connections).
"""

from __future__ import annotations

import json
import os
import pty
import select
import struct
import sys
import time
from pathlib import Path

import pytest

from llm_archive import db
from llm_archive.tui import (
    ArchiveApp,
    ListScreen,
    ShowScreen,
    ThreadRow,
    MessageRow,
    _source_color,
    _SOURCE_COLORS,
    _SOURCE_PALETTE,
    _SUMMARY_SIZES,
    _truncate,
)


# ─── DB fixtures ────────────────────────────────────────────────────


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def _seed_db(con, n_threads: int = 3, sources: list[str] | None = None):
    """Insert n threads with messages + parts into a fresh DB."""
    sources = sources or ["test"]
    now = int(time.time() * 1000)
    for src in sources:
        con.execute("INSERT INTO sources(id) VALUES (?)", (src,))
    for i in range(1, n_threads + 1):
        src = sources[(i - 1) % len(sources)]
        tid = f"{src}:t{i}"
        con.execute(
            "INSERT INTO threads(id, source_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (tid, src, f"Thread {i}", now - i * 1000, now - i * 1000),
        )
        con.execute(
            "INSERT INTO messages(id, thread_id, role, content, content_clean, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"{src}:m{i}a", tid, "user", f"hello world {i}", f"hello world {i}", now - i * 1000),
        )
        con.execute(
            "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
            "VALUES (?,?,?, ?,?,?)",
            (f"{src}:m{i}a", 0, "text", f"hello world {i}", 1, 1),
        )
        con.execute(
            "INSERT INTO messages(id, thread_id, role, content, content_clean, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"{src}:m{i}b", tid, "assistant", f"reply {i}", f"reply {i}", now - i * 1000 + 500),
        )
        con.execute(
            "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
            "VALUES (?,?,?, ?,?,?)",
            (f"{src}:m{i}b", 0, "text", f"reply {i}", 1, 1),
        )
    con.execute(
        "INSERT INTO messages_fts(id, thread_id, content_clean) "
        "SELECT id, thread_id, content_clean FROM messages"
    )
    con.commit()


# ─── Headless Pilot tests ───────────────────────────────────────────


@pytest.fixture
async def app(con):
    """ArchiveApp with a seeded DB, ready for headless pilot testing."""
    _seed_db(con)
    app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
    yield app
    try:
        app.exit()
    except Exception:
        pass


@pytest.fixture
async def multi_app(con):
    """ArchiveApp with multiple sources for color testing."""
    _seed_db(con, n_threads=6, sources=["claude", "chatgpt", "opencode"])
    app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
    yield app
    try:
        app.exit()
    except Exception:
        pass


class TestSourceColor:
    """Per-source brand colors and render output."""

    def test_brand_color_claude(self):
        assert _source_color("claude") == "#D97757"

    def test_brand_color_chatgpt(self):
        assert _source_color("chatgpt") == "#10A37F"

    def test_brand_color_deepseek(self):
        assert _source_color("deepseek") == "#4D6BFE"

    def test_brand_color_gemini(self):
        assert _source_color("gemini") == "#4992EA"

    def test_brand_color_cursor(self):
        assert _source_color("cursor") == "#E5C07B"

    def test_brand_color_windsurf(self):
        assert _source_color("windsurf") == "#67EADA"

    def test_brand_color_opencode(self):
        assert _source_color("opencode") == "#22D3EE"

    def test_all_known_sources_distinct_colors(self):
        colors = list(_SOURCE_COLORS.values())
        unique = set(colors)
        # claude and claudecode intentionally share the same brand color
        assert len(unique) >= len(colors) - 1

    def test_unknown_source_returns_palette_color(self):
        assert _source_color("unknownsrc") in _SOURCE_PALETTE

    def test_deterministic(self):
        assert _source_color("claude") == _source_color("claude")

    def test_different_sources_different_colors(self):
        sources = [s for s in _SOURCE_COLORS if s != "claudecode"]
        colors = {_source_color(s) for s in sources}
        assert len(colors) == len(sources)

    def test_empty_source_does_not_crash(self):
        assert _source_color("") in _SOURCE_PALETTE

    def test_dummy_not_in_source_colors(self):
        assert "dummy" not in _SOURCE_COLORS

    def test_threadrow_render_shows_full_source(self):
        row = ThreadRow(rowid=1, source="chatgpt", title="Hello", updated_at=0)
        text = row.render(width=80)
        assert "chatgpt" in str(text)

    def test_threadrow_render_source_not_truncated(self):
        row = ThreadRow(rowid=1, source="opencode", title="Hello", updated_at=0)
        text = row.render(width=80)
        rendered = str(text)
        assert "ope" not in rendered.replace("opencode", "")
        assert "opencode" in rendered

    def test_threadrow_render_applies_source_color(self):
        row = ThreadRow(rowid=1, source="claude", title="Hello", updated_at=0)
        text = row.render(width=80)
        color = _source_color("claude")
        spans = [s.style for s in text.spans if s.style]
        assert color in str(spans)

    def test_messagerow_render(self):
        row = MessageRow(rowid=1, role="user", snippet="test", created_at=0, thread_rowid=1)
        text = row.render(width=80)
        assert "user" in str(text)

    def test_truncate_zero_limit(self):
        assert _truncate("hello", 0) == ""

    def test_truncate_negative(self):
        assert _truncate("hello", -1) == ""

    def test_truncate_short(self):
        assert _truncate("hi", 80) == "hi"

    def test_truncate_exact(self):
        assert _truncate("hello", 5) == "hello"

    def test_truncate_long(self):
        result = _truncate("hello world", 5)
        assert len(result) == 5
        assert result.endswith("…")


class TestAppStartup:
    """Tests that detect the compose-before-init crash and similar startup errors."""

    async def test_app_mounts_without_error(self, app):
        """The main regression test: app must mount without raising."""
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app._exception, f"App raised: {app._exception}"

    async def test_list_screen_is_active(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ListScreen)

    async def test_listview_exists_after_mount(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.screen.query_one("ListView")
            assert lv is not None

    async def test_threads_loaded(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert len(screen.all_threads) == 3
            assert len(screen.displayed_rows) == 3

    async def test_dummy_source_filtered(self, con):
        _seed_db(con, n_threads=4, sources=["claude", "dummy"])
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            sources = {t.source for t in screen.all_threads}
            assert "dummy" not in sources
            assert "claude" in sources

    async def test_multi_source_loaded(self, multi_app):
        async with multi_app.run_test() as pilot:
            await pilot.pause()
            screen = multi_app.screen
            sources = {t.source for t in screen.all_threads}
            assert sources == {"claude", "chatgpt", "opencode"}

    async def test_multi_source_navigation_and_open(self, multi_app):
        async with multi_app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(multi_app.screen, ShowScreen)


class TestNavigation:
    """j/k + arrow navigation via ListView.index."""

    async def test_cursor_down_j(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.screen.query_one("ListView")
            assert lv.index == 0
            await pilot.press("j")
            await pilot.pause()
            assert lv.index == 1

    async def test_cursor_down_arrow(self, app):
        """Arrow keys handled natively by ListView."""
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.screen.query_one("ListView")
            assert lv.index == 0
            await pilot.press("down")
            await pilot.pause()
            assert lv.index == 1

    async def test_cursor_up_k(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.screen.query_one("ListView")
            await pilot.press("k")
            await pilot.pause()
            assert lv.index == 0

    async def test_cursor_up_arrow(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.screen.query_one("ListView")
            await pilot.press("down")
            await pilot.pause()
            assert lv.index == 1
            await pilot.press("up")
            await pilot.pause()
            assert lv.index == 0

    async def test_cursor_down_clamped_at_end(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            lv = app.screen.query_one("ListView")
            for _ in range(10):
                await pilot.press("j")
                await pilot.pause()
            assert lv.index == 2


class TestSearch:
    """Search/filter behavior."""

    async def test_title_filter(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            screen.search_query = "Thread 2"
            screen._update_display()
            await pilot.pause()
            assert len(screen.displayed_rows) == 1

    async def test_toggle_mode(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.show_deep is False
            await pilot.press("tab")
            await pilot.pause()
            assert screen.show_deep is True

    async def test_clear_search(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            screen.search_query = "nonexistent"
            screen._update_display()
            await pilot.pause()
            assert len(screen.displayed_rows) == 0
            await pilot.press("escape")
            await pilot.pause()
            assert screen.search_query == ""
            assert len(screen.displayed_rows) == 3


class TestThreadView:
    """Opening a thread shows ShowScreen."""

    async def test_open_thread_title_mode(self, app):
        """l opens thread in title filter mode (default)."""
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)

    async def test_open_thread_with_l(self, app):
        """l opens thread in deep mode."""
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)

    async def test_open_thread_with_enter(self, app):
        """Enter handled natively by ListView, fires on_list_view_selected."""
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)

    async def test_back_from_thread_returns_to_list(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ListScreen)


class TestShowScreenContent:
    """ShowScreen renders content safely — markup chars, role labels, markdown."""

    @pytest.fixture(autouse=True)
    def _mock_pager(self, monkeypatch):
        monkeypatch.setattr(ShowScreen, "_open_pager", lambda self: None)

    async def test_brackets_in_content(self, con):
        """Rich markup chars like [array] must not crash rendering."""
        _seed_db(con, n_threads=1)
        con.execute(
            "UPDATE message_parts SET text=? WHERE message_id='test:m1a' AND ord=0",
            ("--remote-allow-origins='*' > /tmp/windsurf-cdp.log 2>&1 &\"'],\n",),
        )
        con.commit()
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)
            assert not app._exception

    async def test_title_with_markup_chars(self, con):
        """Title containing [brackets] must not crash."""
        _seed_db(con, n_threads=1)
        con.execute(
            "UPDATE threads SET title=? WHERE id='test:t1'",
            ("[bold]evil[/bold] title with [1, 2, 3]",),
        )
        con.commit()
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)
            assert not app._exception

    async def test_render_includes_messages(self, app):
        """_render_content produces text with message content."""
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            output = app.screen._render_content(width=80)
            assert "hello world" in output
            assert "reply" in output

    async def test_render_includes_role_labels(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            output = app.screen._render_content(width=80)
            assert "user" in output
            assert "assistant" in output

    async def test_render_has_no_cap(self, con):
        """All messages appear in render — no cap."""
        _seed_db(con, n_threads=1)
        import time
        now = int(time.time() * 1000)
        for i in range(2, 602):
            mid = f"test:extra{i}"
            con.execute(
                "INSERT INTO messages(id, thread_id, role, content, content_clean, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (mid, "test:t1", "user", f"msg {i}", f"msg {i}", now + i),
            )
            con.execute(
                "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
                "VALUES (?,?,?, ?,?,?)",
                (mid, 0, "text", f"message body {i}", 1, 1),
            )
        con.commit()
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            output = app.screen._render_content(width=80)
            assert "message body 2" in output
            assert "message body 601" in output
            assert not app._exception


class TestShowScreenBack:
    """q closes thread view, returns to list — does NOT quit app."""

    @pytest.fixture(autouse=True)
    def _mock_pager(self, monkeypatch):
        monkeypatch.setattr(ShowScreen, "_open_pager", lambda self: None)

    async def test_q_returns_to_list(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, ListScreen)

    async def test_escape_returns_to_list(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ListScreen)


class TestShowScreenSummary:
    """s key cycles through summary sizes, skipping missing ones."""

    @pytest.fixture(autouse=True)
    def _mock_pager(self, monkeypatch):
        monkeypatch.setattr(ShowScreen, "_open_pager", lambda self: None)

    @staticmethod
    def _seed_with_summary(con):
        from llm_archive.db import init_summaries, upsert_thread_summary
        import time
        init_summaries(con)
        upsert_thread_summary(
            con, "test:t1",
            tiny="tiny summary text",
            small="small summary text",
            medium="medium summary text",
            large="large summary text",
            model="test-model",
            summarized_at=int(time.time()),
        )
        con.commit()

    async def test_cycle_to_tiny(self, con):
        _seed_db(con, n_threads=1)
        self._seed_with_summary(con)
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            assert screen._summary_idx == 0
            await pilot.press("s")
            await pilot.pause()
            assert _SUMMARY_SIZES[screen._summary_idx] == "tiny"
            output = screen._render_content(width=80)
            assert "tiny summary text" in output

    async def test_cycle_to_small(self, con):
        _seed_db(con, n_threads=1)
        self._seed_with_summary(con)
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert _SUMMARY_SIZES[screen._summary_idx] == "small"

    async def test_cycle_wraps_to_full(self, con):
        _seed_db(con, n_threads=1)
        self._seed_with_summary(con)
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            for _ in range(len(_SUMMARY_SIZES)):
                await pilot.press("s")
                await pilot.pause()
            assert screen._summary_idx == 0

    async def test_no_summary_stays_full(self, con):
        """Cycling when no summary exists stays on full messages."""
        _seed_db(con, n_threads=1)
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            await pilot.press("s")
            await pilot.pause()
            assert screen._summary_idx == 0
            output = screen._render_content(width=80)
            assert "hello world" in output

    async def test_partial_summary_skips_missing(self, con):
        """Only tiny exists — cycling skips small/medium/large."""
        _seed_db(con, n_threads=1)
        from llm_archive.db import init_summaries, upsert_thread_summary
        import time
        init_summaries(con)
        upsert_thread_summary(
            con, "test:t1",
            tiny="only tiny", small=None, medium=None, large=None,
            model="m", summarized_at=int(time.time()),
        )
        con.commit()
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            await pilot.press("s")
            await pilot.pause()
            assert _SUMMARY_SIZES[screen._summary_idx] == "tiny"
            await pilot.press("s")
            await pilot.pause()
            assert screen._summary_idx == 0  # wraps to full

    async def test_header_shows_size_label(self, con):
        _seed_db(con, n_threads=1)
        self._seed_with_summary(con)
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            assert "⟦" not in str(screen._build_header())
            await pilot.press("s")
            await pilot.pause()
            assert "⟦tiny⟧" in str(screen._build_header())


class TestShowScreenResume:
    """Enter resumes session via browser/CLI command."""

    @pytest.fixture(autouse=True)
    def _mock_pager(self, monkeypatch):
        monkeypatch.setattr(ShowScreen, "_open_pager", lambda self: None)

    async def test_resume_calls_webbrowser(self, con, monkeypatch):
        _seed_db(con, n_threads=1, sources=["chatgpt"])
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert opened

    async def test_resume_unsupported_bells(self, con, monkeypatch):
        _seed_db(con, n_threads=1, sources=["gemini"])
        monkeypatch.setattr("webbrowser.open", lambda url: None)
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ShowScreen)


class TestShowScreenPager:
    """Pager auto-opens on mount and on 'l' key."""

    async def test_pager_called_on_mount(self, con, monkeypatch):
        calls = []
        monkeypatch.setattr(ShowScreen, "_open_pager", lambda self: calls.append(True))
        _seed_db(con, n_threads=1)
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
        assert calls

    async def test_pager_reopened_with_l(self, con, monkeypatch):
        calls = []
        monkeypatch.setattr(ShowScreen, "_open_pager", lambda self: calls.append(True))
        _seed_db(con, n_threads=1)
        app = ArchiveApp(db_path=Path(con.execute("PRAGMA database_list").fetchone()[2]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            initial = len(calls)
            await pilot.press("l")
            await pilot.pause()
            assert len(calls) > initial


class TestEmptyDB:
    """App must handle empty DB without crashing."""

    async def test_empty_db_mounts(self, tmp_path):
        empty_db = tmp_path / "empty.db"
        db.connect(empty_db)  # creates schema
        app = ArchiveApp(db_path=empty_db)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app._exception
            assert len(app.screen.all_threads) == 0


# ─── Asciicast recording (PTY → asciicast v2 JSON) ──────────────────


def record_asciicast(
    db_path: Path,
    output_path: Path,
    *,
    keys: list[tuple[float, bytes]] | None = None,
    duration: float = 3.0,
    width: int = 100,
    height: int = 30,
) -> Path:
    """Run the TUI in a PTY and record output as asciicast v2 JSON.

    Args:
        db_path: Path to the archive DB.
        output_path: Where to write the .cast file.
        keys: Optional list of (delay_seconds, key_bytes) to send.
        duration: Total seconds to record after last key.
        width: Terminal width.
        height: Terminal height.

    Returns:
        Path to the written .cast file.
    """
    keys = keys or []
    env = os.environ.copy()
    env["LLM_ARCHIVE_DB"] = str(db_path)

    header = json.dumps({
        "version": 2,
        "width": width,
        "height": height,
        "timestamp": int(time.time()),
        "env": {"SHELL": "/bin/bash", "TERM": os.environ.get("TERM", "xterm-256color")},
    })

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(
            sys.executable,
            [sys.executable, "-m", "llm_archive", "tui"],
            env,
        )

    struct.pack("HHHH", height, width, 0, 0)
    fcntl_set_size(fd, width, height)

    events: list[list] = []
    start = time.monotonic()

    key_idx = 0
    key_schedule = sorted(keys, key=lambda k: k[0]) if keys else []
    deadline = start + (key_schedule[-1][0] + 1 if key_schedule else duration)

    try:
        while time.monotonic() < deadline:
            rlist = [fd]
            timeout = 0.05

            if key_idx < len(key_schedule):
                elapsed = time.monotonic() - start
                if elapsed >= key_schedule[key_idx][0]:
                    os.write(fd, key_schedule[key_idx][1])
                    key_idx += 1
                    continue
                timeout = min(timeout, key_schedule[key_idx][0] - elapsed)

            readable, _, _ = select.select(rlist, [], [], timeout)
            if fd in readable:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if data:
                    ts = time.monotonic() - start
                    events.append([round(ts, 6), "o", data.decode("utf-8", errors="replace")])
    finally:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

    with open(output_path, "w") as f:
        f.write(header + "\n")
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    return output_path


def fcntl_set_size(fd: int, width: int, height: int) -> None:
    try:
        import fcntl
        import termios

        winsize = struct.pack("HHHH", height, width, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except (ImportError, OSError):
        pass


# ─── Recording tests (marked slow, not in default run) ──────────────


@pytest.fixture
def seeded_db(tmp_path):
    """Create a seeded DB file and return its path."""
    db_path = tmp_path / "record.db"
    con = db.connect(db_path)
    _seed_db(con, n_threads=10)
    con.close()
    return db_path


@pytest.mark.slow
def test_record_startup_asciicast(seeded_db, tmp_path):
    """Record a short TUI session for visual review. Output: .cast file."""
    out = tmp_path / "tui_startup.cast"
    record_asciicast(seeded_db, out, duration=2.0)
    assert out.exists()
    assert out.stat().st_size > 0
    lines = out.read_text().splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2
    assert header["width"] == 100
