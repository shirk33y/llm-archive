"""Performance tests for DB queries, content rendering, search, and TUI startup.

Synthetic data mimics real thread shapes (part counts, text length ranges) from a
production archive. No real conversation data is stored.

Run:    uv run pytest tests/test_perf.py -v -m perf --override-ini='addopts=""'
Profile: uv run pytest tests/test_perf.py -v -m perf -o addopts="" --profile
Large:  uv run pytest tests/test_perf.py -v -m perf -o addopts="" --perf-scale=huge
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ParamSpec, TypeVar, cast

import pytest

from llm_archive import db
from llm_archive.export import render_thread
from llm_archive.tui import _role_separator

pytestmark = pytest.mark.perf

P = ParamSpec("P")
T = TypeVar("T")

_WORDS = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed eiusmod "
          "tempor incididunt ut labore dolore magna aliqua enim minim veniam quis "
          "nostrud exercitation ullamco laboris nisi aliquip ex ea commodo "
          "consequat duis aute irure reprehenderit voluptate velit esse cillum").split()

_WORD_STR = " ".join(_WORDS)


def _lorem(lo: int, hi: int, rng: random.Random) -> str:
    if hi <= 0:
        return ""
    target = rng.randint(lo, hi)
    if target <= len(_WORD_STR):
        return _WORD_STR[:target]
    reps = target // len(_WORD_STR) + 1
    return (_WORD_STR * reps)[:target]


def _seed_thread(con, tid: str, src: str, n_msgs: int, user_ratio: float,
                 parts_per_msg: dict[str, int], len_ranges: dict[str, tuple[int, int]],
                 rng: random.Random, base_ts: int):
    con.execute("INSERT OR IGNORE INTO sources(id) VALUES (?)", (src,))
    con.execute(
        "INSERT INTO threads(id, source_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (tid, src, f"Thread {tid}", base_ts, base_ts),
    )
    for mi in range(1, n_msgs + 1):
        role = "user" if mi <= n_msgs * user_ratio else "assistant"
        mid = f"{tid}:m{mi}"
        created = base_ts + mi
        content = _lorem(20, 200, rng)
        con.execute(
            "INSERT INTO messages(id, thread_id, role, content, content_clean, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (mid, tid, role, content, content, created),
        )
        ord_idx = 0
        for kind, count in parts_per_msg.items():
            lo, hi = len_ranges[kind]
            if count <= 0 or hi <= 0:
                continue
            for _ in range(count):
                ptext = _lorem(lo, hi, rng)
                searchable = 1 if kind == "text" else 0
                con.execute(
                    "INSERT INTO message_parts"
                    "(message_id, ord, kind, text, search_text, visible, searchable, tool_name) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (mid, ord_idx, kind, ptext, ptext if searchable else "",
                     1, searchable, "faktool" if "tool" in kind else ""),
                )
                if searchable:
                    con.execute(
                        "INSERT INTO message_parts_fts(message_id, ord, search_text) VALUES(?,?,?)",
                        (mid, ord_idx, ptext),
                    )
                ord_idx += 1
    con.execute(
        "INSERT INTO messages_fts(id, thread_id, content_clean) "
        "SELECT id, thread_id, content_clean FROM messages WHERE thread_id=?",
        (tid,),
    )


def _seed_mixed_db(con, rng=None):
    rng = rng or random.Random(42)
    now = int(time.time() * 1000)
    profiles = [
        # (n_threads, n_msgs, user_ratio, parts_per_msg, len_ranges)
        (50, 5, 0.5, {"text": 1}, {"text": (20, 200)}),
        (10, 15, 0.5, {"text": 2, "tool_call": 1, "tool_result": 1},
         {"text": (50, 500), "tool_call": (50, 100), "tool_result": (50, 1000)}),
        (2, 100, 0.1, {"text": 1, "tool_call": 2, "tool_result": 2, "reasoning": 1},
         {"text": (50, 1000), "tool_call": (50, 100), "tool_result": (50, 2000), "reasoning": (7, 1500)}),
    ]
    idx = 0
    for n_threads, n_msgs, ratio, parts, ranges in profiles:
        for ti in range(n_threads):
            idx += 1
            _seed_thread(con, f"perf:s{idx}", "perf", n_msgs, ratio, parts, ranges, rng, now - idx * 1000)
    con.commit()


def _seed_xl_thread(con, rng=None):
    rng = rng or random.Random(99)
    now = int(time.time() * 1000)
    _seed_thread(
        con, "perf:xl", "perf", 200, 0.08,
        {"text": 2, "tool_call": 4, "tool_result": 4, "reasoning": 1},
        {"text": (50, 2000), "tool_call": (50, 100), "tool_result": (50, 3000), "reasoning": (7, 2000)},
        rng, now,
    )
    # Plus 50 small threads so list isn't empty
    for i in range(50):
        _seed_thread(con, f"perf:x{i}", "perf", 5, 0.5, {"text": 1}, {"text": (20, 200)}, rng, now - i * 100)
    con.commit()


def _seed_scaled_db(con, scale: str, rng=None):
    rng = rng or random.Random(123)
    now = int(time.time() * 1000)
    profiles = {
        "large": [
            (650, 4, 0.5, {"text": 1}, {"text": (20, 240)}),
            (100, 22, 0.45, {"text": 2, "tool_call": 1, "tool_result": 1},
             {"text": (50, 700), "tool_call": (50, 140), "tool_result": (80, 1400)}),
            (10, 160, 0.1, {"text": 2, "tool_call": 2, "tool_result": 2, "reasoning": 1},
             {"text": (80, 1400), "tool_call": (60, 180), "tool_result": (120, 2400), "reasoning": (20, 1800)}),
        ],
        "huge": [
            (2000, 4, 0.5, {"text": 1}, {"text": (20, 240)}),
            (400, 24, 0.45, {"text": 2, "tool_call": 1, "tool_result": 1},
             {"text": (50, 800), "tool_call": (50, 140), "tool_result": (80, 1600)}),
            (40, 180, 0.1, {"text": 2, "tool_call": 2, "tool_result": 2, "reasoning": 1},
             {"text": (80, 1600), "tool_call": (60, 180), "tool_result": (120, 3000), "reasoning": (20, 2200)}),
        ],
    }
    idx = 0
    for n_threads, n_msgs, ratio, parts, ranges in profiles[scale]:
        for _ in range(n_threads):
            idx += 1
            _seed_thread(
                con,
                f"perf:{scale}:{idx}",
                "perf",
                n_msgs,
                ratio,
                parts,
                ranges,
                rng,
                now - idx * 1000,
            )
    con.commit()


def _db_stats(con: sqlite3.Connection) -> dict[str, int]:
    stats = {}
    for table in ("threads", "messages", "message_parts"):
        stats[table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    page_count = int(con.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(con.execute("PRAGMA page_size").fetchone()[0])
    stats["bytes"] = page_count * page_size
    return stats


def _thread_row(con, tid: str) -> dict:
    row = con.execute(
        "SELECT id, source_id, title, created_at, updated_at FROM threads WHERE id=?",
        (tid,),
    ).fetchone()
    return dict(row)


def _largest_thread_row(con: sqlite3.Connection) -> dict:
    row = con.execute(
        """
        SELECT t.id, t.source_id, t.title, t.created_at, t.updated_at
        FROM threads t
        JOIN messages m ON m.thread_id = t.id
        GROUP BY t.id
        ORDER BY COUNT(m.id) DESC, t.updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row)


def _timed(fn: Callable[P, T], *a: P.args, **kw: P.kwargs) -> tuple[float, T]:
    t0 = time.perf_counter()
    r = fn(*a, **kw)
    return time.perf_counter() - t0, r


def _explain(con: sqlite3.Connection, sql: str, params=()) -> list[str]:
    return [str(row["detail"]) for row in con.execute(f"EXPLAIN QUERY PLAN {sql}", params)]


def _uses_scan(plan: list[str]) -> bool:
    return any(
        "SCAN " in row and "USING INDEX" not in row and "VIRTUAL TABLE" not in row
        for row in plan
    )


def _write_markdown(path: Path, target_bytes: int) -> None:
    header = "# Glow Perf Probe\n\n"
    line = "body line of content for glow startup profiling\n"
    chunks = [header]
    written = len(header.encode())
    line_bytes = len(line.encode())
    while written + line_bytes < target_bytes:
        chunks.append(line)
        written += line_bytes
    path.write_text("".join(chunks), encoding="utf-8")


def _write_headless_battle_markdown(path: Path, target_bytes: int = 267_000) -> None:
    header = (
        "<!-- thread:opencode:synthetic-headless-battle-seams source:opencode -->\n"
        "# Map headless battle seams (@explore subagent)\n\n"
    )
    user = "## user · 1\n\nMap headless battle seams for the browser combat harness.\n\n"
    tool_call = "**▸ bash**\n\n```bash\nrg -n \"battle|headless|seam\" js tests docs\n```\n\n"
    tool_result = (
        "**◀ result**\n\n"
        "js/battle/engine.js: resolve queued card activations during headless runs\n"
        "js/battle/animations.js: skip visual frame waits under the test harness\n"
        "tests/battle/headless.test.js: verify battle seams without DOM timing\n"
    )
    reasoning = (
        "*reasoning:* The useful seams are state construction, battle loop stepping, "
        "animation adapters, and deterministic RNG injection.\n\n"
    )
    assistant = (
        "## assistant · 2\n\n"
        "Findings:\n"
        "- use the battle state factory as the headless entry point\n"
        "- keep animation promises behind an adapter\n"
        "- assert scoring after each deterministic step\n\n"
    )
    unit = user + tool_call + tool_result + "\n" + reasoning + assistant
    chunks = [header]
    written = len(header.encode())
    unit_bytes = len(unit.encode())
    while written + unit_bytes < target_bytes:
        chunks.append(unit)
        written += unit_bytes
    chunks.append(unit[: max(target_bytes - written, 0)])
    path.write_text("".join(chunks), encoding="utf-8")


def _drive_terminal_probe(data: bytes, master: int) -> None:
    import os

    text = data.decode("utf-8", "replace")
    if "\x1b]11;?" in text:
        os.write(master, b"\x1b]11;rgb:0000/0000/0000\x1b\\")
    if "\x1b[6n" in text:
        os.write(master, b"\x1b[1;1R")


def _measure_glow_first_content(path: Path, needle: bytes) -> dict[str, float | int]:
    import os
    import select
    import shutil
    from tests._pty import spawn_pty

    glow_bin = shutil.which("glow")
    assert glow_bin is not None
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    spawn_start = time.perf_counter()
    proc, master = spawn_pty(
        [glow_bin, "-p", "-s", "auto", "-w", "100", str(path)],
        env=env,
        width=100,
        height=30,
    )
    spawned = time.perf_counter()
    first_output = None
    first_content = None
    buf = b""
    deadline = spawn_start + 5
    try:
        while time.perf_counter() < deadline:
            readable, _, _ = select.select([master], [], [], 0.02)
            if master not in readable:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            now = time.perf_counter()
            first_output = first_output or now
            buf += data
            _drive_terminal_probe(data, master)
            if needle in buf:
                first_content = now
                break
    finally:
        try:
            os.write(master, b"q")
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
        os.close(master)

    assert first_output is not None, f"glow produced no output for {path}"
    assert first_content is not None, f"glow rendered no content for {path}"
    return {
        "spawn": spawned - spawn_start,
        "first_output": first_output - spawn_start,
        "first_content": first_content - spawn_start,
        "bytes": path.stat().st_size,
    }


# ─── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def perf_con(tmp_path):
    c = db.connect(tmp_path / "perf.db")
    yield c
    c.close()


@pytest.fixture
def mixed_db(perf_con):
    _seed_mixed_db(perf_con)
    return perf_con


@pytest.fixture
def xl_db(perf_con):
    _seed_xl_thread(perf_con)
    return perf_con


@pytest.fixture
def scaled_db(perf_con, request):
    _seed_scaled_db(perf_con, request.config.getoption("--perf-scale"))
    return perf_con


@pytest.fixture
def tol(request):
    """Multiply timing thresholds when cProfile overhead is active."""
    return 3.0 if request.config.getoption("--profile", default=False) else 1.0


# ─── DB layer ──────────────────────────────────────────────────────────────────

class TestDB:
    def test_list_threads(self, mixed_db, tol):
        elapsed, threads = _timed(db.list_threads, mixed_db)
        assert elapsed < 0.1 * tol, f"list_threads {elapsed:.3f}s"
        assert len(threads) > 50

    def test_fetch_xl_thread(self, xl_db, tol):
        thread = _thread_row(xl_db, "perf:xl")
        elapsed, data = _timed(db._fetch_thread_data, xl_db, thread)
        assert elapsed < 1.0 * tol, f"_fetch_thread_data {elapsed:.3f}s"
        part_count = sum(len(m.get("parts", [])) for m in data["messages"])
        assert part_count > 1000, f"only {part_count} parts"

    def test_fetch_repeated(self, xl_db, tol):
        thread = _thread_row(xl_db, "perf:xl")
        times = [_timed(db._fetch_thread_data, xl_db, thread)[0] for _ in range(3)]
        avg = sum(times) / 3
        assert avg < 1.0 * tol, f"avg fetch {avg:.3f}s"

    def test_search(self, mixed_db, tol):
        elapsed, results = _timed(db.search_messages, mixed_db, "lorem", limit=50)
        assert elapsed < 0.5 * tol, f"search {elapsed:.3f}s"
        assert len(results) > 0

    def test_search_threads(self, mixed_db, tol):
        elapsed, _ = _timed(db.search_threads, mixed_db, "lorem", limit=50)
        assert elapsed < 0.5 * tol, f"search_threads {elapsed:.3f}s"

    def test_search_no_results(self, mixed_db, tol):
        elapsed, _ = _timed(db.search_messages, mixed_db, "zzzznotfound", limit=50)
        assert elapsed < 0.2 * tol, f"empty search {elapsed:.3f}s"


# ─── Render layer ───────────────────────────────────────────────────────────────

class TestRender:
    def test_render_xl(self, xl_db, tol):
        from llm_archive.tui import _render_thread_content
        thread = _thread_row(xl_db, "perf:xl")
        data = db._fetch_thread_data(xl_db, thread)
        elapsed, output = _timed(_render_thread_content, data, xl_db, width=120)
        assert elapsed < 0.5 * tol, f"render {elapsed:.3f}s"
        assert len(output) > 5000

    def test_render_xl_verbose(self, xl_db, tol):
        from llm_archive.tui import _render_thread_content
        thread = _thread_row(xl_db, "perf:xl")
        data = db._fetch_thread_data(xl_db, thread)
        elapsed, output = _timed(_render_thread_content, data, xl_db, width=120, verbose=True)
        assert elapsed < 1.0 * tol, f"verbose render {elapsed:.3f}s"
        assert len(output) > 20000

    def test_role_separator_perf(self, tol):
        t0 = time.perf_counter()
        for i in range(1000):
            _role_separator("assistant", i, "9m", 120)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.05 * tol, f"1000 separators {elapsed:.3f}s"


class TestProfile:
    def test_export_render_profile(self, xl_db, tol, perf_report):
        data = db.get_thread(xl_db, "perf:xl")
        assert data is not None
        elapsed, output = _timed(render_thread, data)
        perf_report.metric("export.render_thread.profiled", elapsed, bytes=len(output))
        assert elapsed < 0.5 * tol, f"export render profile {elapsed:.3f}s"
        assert len(output) > 5000

    def test_scaled_archive_sqlite_profile(self, scaled_db, tol, perf_report, request):
        scale = request.config.getoption("--perf-scale")
        stats = _db_stats(scaled_db)
        perf_report.metric("scaled_archive.size", 0.0, scale=scale, **stats)
        perf_report.insight(
            f"{scale} synthetic archive: {stats['threads']} threads, "
            f"{stats['messages']} messages, {stats['message_parts']} parts, "
            f"{stats['bytes'] / (1024 * 1024):.1f} MiB sqlite"
        )

        largest = _largest_thread_row(scaled_db)
        operations = [
            ("db.list_threads.limit50", lambda: db.list_threads(scaled_db, limit=50), 0.15),
            ("db.search_messages.limit50", lambda: db.search_messages(scaled_db, "lorem", limit=50), 0.8),
            ("db.search_threads.limit50", lambda: db.search_threads(scaled_db, "lorem", limit=50), 0.8),
            ("db.fetch_largest_thread", lambda: db._fetch_thread_data(scaled_db, largest), 1.5),
        ]
        timings = []
        for name, operation, threshold in operations:
            elapsed, result = _timed(operation)
            size = len(result) if hasattr(result, "__len__") else 0
            timings.append((name, elapsed))
            perf_report.metric(name, elapsed, rows=size, scale=scale)
            assert elapsed < threshold * tol, f"{name} {elapsed:.3f}s"

        slowest_name, slowest_seconds = max(timings, key=lambda row: row[1])
        perf_report.insight(f"slowest scaled DB operation: {slowest_name} at {slowest_seconds:.4f}s")

        plans = {
            "list_threads": _explain(
                scaled_db,
                """
                SELECT source_id, id AS thread_id, title, updated_at, rowid AS thread_rowid
                FROM threads
                ORDER BY updated_at DESC
                LIMIT 50
                """,
            ),
            "fetch_messages": _explain(
                scaled_db,
                "SELECT id, role, created_at FROM messages WHERE thread_id=? ORDER BY created_at, id",
                (largest["id"],),
            ),
            "fetch_parts": _explain(
                scaled_db,
                """
                SELECT message_parts.message_id, ord, kind, text, data, visible, searchable,
                    tool_name, tool_input, tool_result, tool_is_error
                FROM message_parts
                JOIN messages ON messages.id = message_parts.message_id
                WHERE messages.thread_id=?
                ORDER BY messages.created_at, messages.id, ord
                """,
                (largest["id"],),
            ),
            "search_messages": _explain(
                scaled_db,
                """
                SELECT p.message_id, p.ord
                FROM message_parts_fts f
                JOIN message_parts p ON p.message_id = f.message_id AND p.ord = f.ord
                WHERE message_parts_fts MATCH ?
                LIMIT 50
                """,
                (db._fts_query("lorem"),),
            ),
        }
        for name, plan in plans.items():
            perf_report.metric(f"sqlite.plan.{name}", 0.0, plan=plan, uses_scan=_uses_scan(plan))
            if _uses_scan(plan):
                perf_report.insight(f"SQLite plan for {name} includes a table scan: {' | '.join(plan)}")


# ─── TUI startup + list render ─────────────────────────────────────────────────

class TestTUIStartup:
    async def test_app_startup_and_list(self, mixed_db, monkeypatch, tol, perf_report):
        from pathlib import Path as P
        from llm_archive.tui import ArchiveApp
        monkeypatch.setattr("llm_archive.tui._open_thread_pager", lambda *a, **k: None)
        db_path = mixed_db.execute("PRAGMA database_list").fetchone()[2]
        app = ArchiveApp(db_path=P(db_path))
        t0 = time.perf_counter()
        async with app.run_test() as pilot:
            await pilot.pause()
            elapsed = time.perf_counter() - t0
            assert elapsed < 2.0 * tol, f"startup+list {elapsed:.3f}s"
            options = cast(Any, app.screen.query_one("OptionList"))
            perf_report.metric("tui.startup_list.mixed", elapsed, rows=options.option_count)
            assert options.option_count > 50

    async def test_scaled_app_startup_and_list(
        self,
        scaled_db,
        monkeypatch,
        tol,
        perf_report,
        request,
    ):
        from pathlib import Path as P
        from llm_archive.tui import ArchiveApp

        monkeypatch.setattr("llm_archive.tui._open_thread_pager", lambda *a, **k: None)
        db_path = scaled_db.execute("PRAGMA database_list").fetchone()[2]
        app = ArchiveApp(db_path=P(db_path))
        scale = request.config.getoption("--perf-scale")

        t0 = time.perf_counter()
        async with app.run_test() as pilot:
            await pilot.pause()
            elapsed = time.perf_counter() - t0
            options = cast(Any, app.screen.query_one("OptionList"))
            rows = options.option_count

        perf_report.metric("tui.startup_list.scaled", elapsed, rows=rows, scale=scale)
        assert rows == min(500, _db_stats(scaled_db)["threads"])
        assert elapsed < 3.0 * tol, f"scaled startup+list {elapsed:.3f}s"

    async def test_list_render_many_threads(self, perf_con, monkeypatch, perf_report):
        from pathlib import Path as P
        from llm_archive.tui import ArchiveApp
        monkeypatch.setattr("llm_archive.tui._open_thread_pager", lambda *a, **k: None)
        rng = random.Random(7)
        now = int(time.time() * 1000)
        for i in range(500):
            _seed_thread(
                perf_con, f"perf:l{i}", "perf", 2, 0.5,
                {"text": 1}, {"text": (20, 80)}, rng, now - i,
            )
        perf_con.commit()
        db_path = perf_con.execute("PRAGMA database_list").fetchone()[2]
        app = ArchiveApp(db_path=P(db_path))
        t0 = time.perf_counter()
        async with app.run_test() as pilot:
            await pilot.pause()
            elapsed = time.perf_counter() - t0
            options = cast(Any, app.screen.query_one("OptionList"))
            perf_report.metric("tui.list_render.500_threads", elapsed, rows=options.option_count)
            assert options.option_count > 100

    async def test_open_thread_from_list(self, mixed_db, monkeypatch, tol, perf_report):
        from pathlib import Path as P
        from llm_archive.tui import ArchiveApp, ListScreen
        monkeypatch.setattr("llm_archive.tui._open_thread_pager", lambda *a, **k: None)
        db_path = mixed_db.execute("PRAGMA database_list").fetchone()[2]
        app = ArchiveApp(db_path=P(db_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ListScreen)
            t0 = time.perf_counter()
            await pilot.press("l")
            await pilot.pause()
            elapsed = time.perf_counter() - t0
            perf_report.metric("tui.open_selected_thread", elapsed)
            assert elapsed < 2.0 * tol, f"open thread {elapsed:.3f}s"

    def test_cli_startup_e2e(self, perf_con, tol, perf_report):
        """Full CLI startup: interpreter + imports + DB connect + list render."""
        import os
        import select
        import shutil
        from tests._pty import spawn_pty

        rng = random.Random(42)
        now = int(time.time() * 1000)
        for i in range(20):
            _seed_thread(
                perf_con, f"perf:e2e{i}", "perf", 3, 0.5,
                {"text": 1}, {"text": (20, 100)}, rng, now - i * 1000,
            )
        perf_con.commit()
        db_file = perf_con.execute("PRAGMA database_list").fetchone()[2]
        perf_con.close()

        bin_path = shutil.which("llm-archive")
        assert bin_path, "llm-archive not on PATH"

        env = os.environ.copy()
        env["LLM_ARCHIVE_DB"] = db_file
        env["TERM"] = "xterm-256color"
        env.pop("LLM_ARCHIVE_CONFIG", None)

        proc, master = spawn_pty([bin_path, "tui"], env=env, width=100, height=30)
        t0 = time.perf_counter()

        buf = b""
        deadline = t0 + 10
        rendered = False
        while time.perf_counter() < deadline:
            r, _, _ = select.select([master], [], [], 0.02)
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if buf.count(b"\xe2\x96\xb6") >= 3:
                    rendered = True
                    break

        elapsed = time.perf_counter() - t0
        proc.kill()
        proc.wait(timeout=3)
        os.close(master)

        assert rendered, f"list not rendered in {elapsed:.1f}s ({len(buf)}b output)"
        perf_report.metric("cli.tui.first_list_paint", elapsed, bytes=len(buf))
        assert elapsed < 3.0 * tol, f"CLI startup {elapsed:.3f}s"

    def test_cli_open_selected_thread_real_glow_e2e(self, perf_con, tmp_path, tol, perf_report):
        import os
        import select
        import shutil
        from tests._pty import spawn_pty
        from llm_archive import export

        bin_path = shutil.which("llm-archive")
        assert bin_path, "llm-archive not on PATH"
        assert shutil.which("glow"), "glow not on PATH"

        rng = random.Random(11)
        now = int(time.time() * 1000)
        thread_id = "perf:headless-battle"
        title = "Map headless battle seams (@explore subagent)"
        marker = "HEADLESS_BATTLE_GLOW_CONTENT"
        _seed_thread(
            perf_con,
            thread_id,
            "opencode",
            9,
            0.45,
            {"text": 2, "tool_call": 1, "tool_result": 5, "reasoning": 1},
            {
                "text": (500, 1200),
                "tool_call": (80, 180),
                "tool_result": (1800, 5200),
                "reasoning": (500, 1800),
            },
            rng,
            now,
        )
        perf_con.execute("UPDATE threads SET title=? WHERE id=?", (title, thread_id))
        perf_con.execute(
            "UPDATE messages SET content=?, content_clean=? WHERE id=(SELECT id FROM messages WHERE thread_id=? LIMIT 1)",
            (marker, marker, thread_id),
        )
        perf_con.execute(
            """
            UPDATE message_parts
            SET text=?, search_text=?
            WHERE rowid=(
                SELECT mp.rowid
                FROM message_parts mp
                JOIN messages m ON m.id = mp.message_id
                WHERE m.thread_id=? AND mp.visible=1
                LIMIT 1
            )
            """,
            (marker, marker, thread_id),
        )
        perf_con.commit()
        db_file = perf_con.execute("PRAGMA database_list").fetchone()[2]

        config_path = tmp_path / "config.toml"
        export_dir = tmp_path / "exports"
        config_path.write_text(f"[export]\ndir = {str(export_dir)!r}\n", encoding="utf-8")
        export.write_thread(
            perf_con,
            thread_id,
            "opencode",
            SimpleNamespace(export=SimpleNamespace(dir=str(export_dir), auto=True)),
            force=True,
        )

        env = os.environ.copy()
        env["LLM_ARCHIVE_DB"] = db_file
        env["LLM_ARCHIVE_CONFIG"] = str(config_path)
        env["TERM"] = "xterm-256color"

        proc, master = spawn_pty([bin_path, "tui"], env=env, width=100, height=30)
        start = time.perf_counter()
        buf = b""
        opened_at = None
        content_at = None
        pressed_at = None
        deadline = start + 10
        try:
            while time.perf_counter() < deadline:
                readable, _, _ = select.select([master], [], [], 0.02)
                if master not in readable:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                _drive_terminal_probe(chunk, master)
                now_time = time.perf_counter()
                if pressed_at is None and title.encode() in buf:
                    pressed_at = now_time
                    os.write(master, b"l")
                    buf = b""
                    continue
                if pressed_at is None:
                    continue
                if opened_at is None and b'Opening "Map headless battle seams' in buf:
                    opened_at = now_time
                if marker.encode() in buf:
                    content_at = now_time
                    break
        finally:
            try:
                os.write(master, b"q")
            except OSError:
                pass
            proc.kill()
            proc.wait(timeout=3)
            os.close(master)

        assert pressed_at is not None, "thread list never rendered target thread"
        assert opened_at is not None, "opening screen did not render after pressing l"
        assert content_at is not None, "glow did not render target thread content"
        opening_seconds = opened_at - pressed_at
        content_seconds = content_at - pressed_at
        perf_report.metric("cli.open_selected_thread.opening_screen", opening_seconds)
        perf_report.metric("cli.open_selected_thread.glow_content", content_seconds)
        perf_report.insight(
            f"full CLI open path: opening screen {opening_seconds:.4f}s after l, "
            f"glow content {content_seconds:.4f}s after l"
        )
        assert opening_seconds < 0.35 * tol, f"opening screen after {opening_seconds:.3f}s"
        assert content_seconds < 1.0 * tol, f"glow content after {content_seconds:.3f}s"

    def test_glow_viewer_cache_miss(self, xl_db, monkeypatch, tmp_path, tol, perf_report):
        from llm_archive import export
        from llm_archive.tui import _open_thread_pager

        class _Suspend:
            def __enter__(self):
                return None

            def __exit__(self, *_args):
                return None

        class _App:
            console = SimpleNamespace(size=SimpleNamespace(width=100))

            def suspend(self):
                return _Suspend()

        data = db.get_thread(xl_db, "perf:xl")
        assert data is not None

        config = SimpleNamespace(
            export=SimpleNamespace(dir=str(tmp_path / "exports"), auto=True)
        )
        glow_calls: list[float] = []
        started = 0.0

        def fake_view(_path, *, width):
            assert width == 98
            glow_calls.append(time.perf_counter() - started)
            return 0

        monkeypatch.setattr("llm_archive.config.load_config", lambda: config)
        monkeypatch.setattr("llm_archive.glow.is_available", lambda: True)
        monkeypatch.setattr("llm_archive.glow.is_too_large", lambda _path: False)
        monkeypatch.setattr("llm_archive.glow.view", fake_view)

        def open_viewer():
            nonlocal started
            started = time.perf_counter()
            _open_thread_pager(cast(Any, _App()), data, xl_db)

        miss_elapsed, _ = _timed(open_viewer)
        assert glow_calls, "glow.view was not reached on cache miss"
        miss_before_glow = glow_calls[-1]

        md_path = export.thread_md_path("perf", "perf:xl", cast(Any, config))
        assert md_path.exists()
        size = md_path.stat().st_size

        hit_elapsed, _ = _timed(open_viewer)
        hit_before_glow = glow_calls[-1]

        perf_report.metric(
            "viewer.glow.cache_miss.before_glow",
            miss_before_glow,
            bytes=size,
        )
        perf_report.metric("viewer.glow.cache_miss.total", miss_elapsed, bytes=size)
        perf_report.metric("viewer.glow.cache_hit.before_glow", hit_before_glow, bytes=size)
        perf_report.metric("viewer.glow.cache_hit.total", hit_elapsed, bytes=size)
        if miss_before_glow > hit_before_glow * 5:
            perf_report.insight(
                f"viewer cache miss delays glow by {miss_before_glow:.4f}s vs "
                f"{hit_before_glow:.4f}s cache hit; export/render path is the likely terminal flash"
            )

        assert miss_before_glow < 1.0 * tol, f"viewer cache miss before glow {miss_before_glow:.3f}s"
        assert hit_before_glow < 0.1 * tol, f"viewer cache hit before glow {hit_before_glow:.3f}s"

    def test_viewer_stage_timings_without_terminal_suspend(
        self, xl_db, monkeypatch, tmp_path, tol, perf_report
    ):
        from llm_archive import export, tui
        from llm_archive.tui import _open_thread_pager

        class _Suspend:
            def __enter__(self):
                return None

            def __exit__(self, *_args):
                return None

        class _App:
            console = SimpleNamespace(size=SimpleNamespace(width=100))

            def suspend(self):
                return _Suspend()

        data = db.get_thread(xl_db, "perf:xl")
        assert data is not None

        config = SimpleNamespace(
            export=SimpleNamespace(dir=str(tmp_path / "exports"), auto=True)
        )
        started = 0.0
        opening_calls: list[float] = []
        stub_calls: list[float] = []
        glow_calls: list[float] = []

        real_stub = tui._ensure_thread_stub

        def fake_show_opening_screen(title: str, width: int) -> None:
            opening_calls.append(time.perf_counter() - started)

        def fake_ensure_thread_stub(md_path, thread_id: str, source_id: str, title: str) -> None:
            stub_calls.append(time.perf_counter() - started)
            real_stub(md_path, thread_id, source_id, title)

        def fake_view(_path, *, width):
            assert width == 98
            glow_calls.append(time.perf_counter() - started)
            return 0

        monkeypatch.setattr("llm_archive.config.load_config", lambda: config)
        monkeypatch.setattr("llm_archive.glow.is_available", lambda: True)
        monkeypatch.setattr("llm_archive.glow.is_too_large", lambda _path: False)
        monkeypatch.setattr("llm_archive.glow.view", fake_view)
        monkeypatch.setattr("llm_archive.tui._show_opening_screen", fake_show_opening_screen)
        monkeypatch.setattr("llm_archive.tui._ensure_thread_stub", fake_ensure_thread_stub)

        def open_viewer():
            nonlocal started
            started = time.perf_counter()
            _open_thread_pager(cast(Any, _App()), data, xl_db)

        elapsed, _ = _timed(open_viewer)
        md_path = export.thread_md_path("perf", "perf:xl", cast(Any, config))
        assert md_path.exists()
        assert opening_calls, "opening screen was not shown"
        assert stub_calls, "stub was not written"
        assert glow_calls, "glow.view was not reached"

        perf_report.metric("viewer.stage.opening_screen", opening_calls[-1], bytes=md_path.stat().st_size)
        perf_report.metric("viewer.stage.stub_write", stub_calls[-1], bytes=md_path.stat().st_size)
        perf_report.metric("viewer.stage.glow_call", glow_calls[-1], bytes=md_path.stat().st_size)
        perf_report.metric("viewer.stage.total", elapsed, bytes=md_path.stat().st_size)
        perf_report.insight(
            f"viewer stages without terminal suspend: opening {opening_calls[-1]:.4f}s, "
            f"stub {stub_calls[-1]:.4f}s, glow {glow_calls[-1]:.4f}s"
        )

        assert opening_calls[-1] < 0.05 * tol, f"opening screen {opening_calls[-1]:.3f}s"
        assert stub_calls[-1] < 0.1 * tol, f"stub write {stub_calls[-1]:.3f}s"
        assert glow_calls[-1] < 0.15 * tol, f"glow call {glow_calls[-1]:.3f}s"

    @pytest.mark.skipif(__import__("shutil").which("glow") is None, reason="glow not installed")
    def test_real_glow_first_paint_profile(self, tmp_path, perf_report, tol):
        import shutil

        assert shutil.which("glow") is not None

        cases = (
            ("small", 4_096, b"body line of content"),
            ("headless_battle_synthetic", 267_000, b"Map headless battle seams"),
            ("near_limit", 900_000, b"body line of content"),
        )
        for label, size, needle in cases:
            md = tmp_path / f"{label}.md"
            if label == "headless_battle_synthetic":
                _write_headless_battle_markdown(md, size)
            else:
                _write_markdown(md, size)

            metrics = _measure_glow_first_content(md, needle)
            spawn_seconds = float(metrics["spawn"])
            first_output_seconds = float(metrics["first_output"])
            first_content_seconds = float(metrics["first_content"])
            byte_count = int(metrics["bytes"])
            perf_report.metric(f"glow.{label}.spawn", spawn_seconds, bytes=byte_count)
            perf_report.metric(
                f"glow.{label}.first_output",
                first_output_seconds,
                bytes=byte_count,
            )
            perf_report.metric(
                f"glow.{label}.first_content",
                first_content_seconds,
                bytes=byte_count,
            )
            perf_report.insight(
                f"glow {label}: spawn {spawn_seconds:.4f}s, first output "
                f"{first_output_seconds:.4f}s, first content {first_content_seconds:.4f}s"
            )
            assert first_content_seconds < 2.0 * tol, (
                f"glow {label} first content {first_content_seconds:.3f}s"
            )

    @pytest.mark.skipif(__import__("shutil").which("glow") is None, reason="glow not installed")
    def test_real_thread_matches_headless_battle_synthetic(self, request, tmp_path, perf_report):
        real_thread_id = request.config.getoption("--perf-real-thread-id")
        if not real_thread_id:
            pytest.skip("pass --perf-real-thread-id to compare a live read-only thread")

        live_db = Path.home() / ".llm-archive" / "archive.db"
        con = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT id, source_id, title, created_at, updated_at FROM threads WHERE id=?",
                (real_thread_id,),
            ).fetchone()
            assert row is not None, f"thread not found: {real_thread_id}"
            t_fetch, data = _timed(db._fetch_thread_data, con, dict(row))
            t_render, real_md = _timed(render_thread, data)
        finally:
            con.close()

        real_path = tmp_path / "real.md"
        real_path.write_text(real_md, encoding="utf-8")
        synthetic_path = tmp_path / "synthetic.md"
        _write_headless_battle_markdown(synthetic_path, real_path.stat().st_size)

        real_metrics = _measure_glow_first_content(real_path, b"Map headless battle seams")
        synthetic_metrics = _measure_glow_first_content(
            synthetic_path,
            b"Map headless battle seams",
        )
        real_first_content = float(real_metrics["first_content"])
        synthetic_first_content = float(synthetic_metrics["first_content"])
        delta = abs(real_first_content - synthetic_first_content)
        perf_report.metric("real_thread.fetch", t_fetch, thread_id=real_thread_id)
        perf_report.metric(
            "real_thread.export_render",
            t_render,
            bytes=real_path.stat().st_size,
            thread_id=real_thread_id,
        )
        perf_report.metric(
            "glow.real_headless_battle.first_content",
            real_first_content,
            bytes=int(real_metrics["bytes"]),
        )
        perf_report.metric(
            "glow.synthetic_headless_battle.first_content",
            synthetic_first_content,
            bytes=int(synthetic_metrics["bytes"]),
        )
        perf_report.insight(
            f"real headless battle thread: fetch {t_fetch:.4f}s, export render {t_render:.4f}s, "
            f"glow first content {real_first_content:.4f}s"
        )
        perf_report.insight(
            f"synthetic headless battle glow first content {synthetic_first_content:.4f}s; "
            f"delta vs real {delta:.4f}s"
        )
        assert delta < 0.25, f"synthetic glow first-content differs from real by {delta:.3f}s"


# ─── Full pipeline ─────────────────────────────────────────────────────────────

class TestPipeline:
    def test_open_xl_pipeline(self, xl_db, tol):
        from llm_archive.tui import _render_thread_content
        t_list, threads = _timed(db.list_threads, xl_db)
        thread = _thread_row(xl_db, "perf:xl")
        t_fetch, data = _timed(db._fetch_thread_data, xl_db, thread)
        t_render, _ = _timed(_render_thread_content, data, xl_db, width=120)
        total = t_list + t_fetch + t_render
        assert total < 2.0 * tol, f"pipeline {total:.3f}s (list={t_list:.3f} fetch={t_fetch:.3f} render={t_render:.3f})"

    def test_open_multiple_threads(self, mixed_db, tol):
        threads = db.list_threads(mixed_db)
        total = 0.0
        for t in threads[:10]:
            e_fetch, _ = _timed(db.get_thread, mixed_db, t["thread_id"])
            total += e_fetch
        assert total < 1.0 * tol, f"10 threads {total:.3f}s"


# ─── Append-only export verification ─────────────────────────────────────────
@pytest.mark.perf
class TestAppendOnlyExport:
    def test_append_only_does_not_rewrite_full_content(self, tmp_path, perf_con, perf_report):
        """Prove append-only writes only new messages, not full thread content."""
        from llm_archive import export, db
        from llm_archive.schema import IngestedThread, IngestedMessage, IngestedPart

        config = SimpleNamespace(
            export=SimpleNamespace(dir=str(tmp_path / "exports"), auto=True)
        )
        
        # Initial thread with 3 messages
        initial = IngestedThread(
            id="append:perf",
            source_id="perf",
            title="Append performance test",
            created_at=1700000000000,
            updated_at=1700000100000,
            messages=[
                IngestedMessage(
                    id=i,
                    thread_id="append:perf",
                    role="user" if i % 2 == 0 else "assistant",
                    created_at=1700000000000 + i * 1000,
                    content=f"message {i}",
                    parts=[IngestedPart(kind="text", text=f"chunk {i}")],
                )
                for i in range(3)
            ],
        )
        db.save_thread(perf_con, initial)
        
        # Initial export
        t0 = time.perf_counter()
        path1 = export.write_thread(perf_con, "append:perf", "perf", config, force=True)
        write_time_1 = time.perf_counter() - t0
        content1 = path1.read_text()
        size1 = path1.stat().st_size
        msg_markers_1 = content1.count("<!-- msg:")

        # Add 2 more messages
        updated = IngestedThread(
            id="append:perf",
            source_id="perf",
            title="Append performance test",
            created_at=1700000000000,
            updated_at=1700000200000,
            messages=[
                IngestedMessage(
                    id=i,
                    thread_id="append:perf",
                    role="user" if i % 2 == 0 else "assistant",
                    created_at=1700000000000 + i * 1000,
                    content=f"message {i}",
                    parts=[IngestedPart(kind="text", text=f"chunk {i}")],
                )
                for i in range(5)
            ],
        )
        db.save_thread(perf_con, updated)

        # Append-only export should be much faster than full rewrite
        t0 = time.perf_counter()
        path2 = export.write_thread(perf_con, "append:perf", "perf", config, force=True)
        write_time_2 = time.perf_counter() - t0
        content2 = path2.read_text()
        size2 = path2.stat().st_size
        msg_markers_2 = content2.count("<!-- msg:")

        perf_report.metric("append_only.write_first", write_time_1, bytes=size1, messages=msg_markers_1)
        perf_report.metric("append_only.write_append", write_time_2, bytes=size2, messages=msg_markers_2)
        perf_report.metric("append_only.size_growth", size2 - size1, delta_messages=msg_markers_2 - msg_markers_1)

        assert path1 == path2, "same path returned"
        assert content2.startswith(content1), "content starts with original"
        assert msg_markers_1 == 3, "first export has 3 messages"
        assert msg_markers_2 == 5, "second export has 5 messages"
        assert size2 > size1, "file grew with new messages"
        assert write_time_2 < write_time_1 * 0.8, f"append {write_time_2:.4f}s should be faster than rewrite {write_time_1:.4f}s"

    def test_viewer_stub_only_creates_minimal_content(self, xl_db, tmp_path, monkeypatch, perf_report):
        """Prove stub creation writes minimal content, not full thread export."""
        from llm_archive import export
        from llm_archive.tui import _open_thread_pager

        config = SimpleNamespace(
            export=SimpleNamespace(dir=str(tmp_path / "exports"), auto=True)
        )
        data = db.get_thread(xl_db, "perf:xl")
        assert data is not None

        md_path = export.thread_md_path("perf", "perf:xl", config)
        assert not md_path.exists(), "file should not exist yet"

        stub_writes = []
        original_write = Path.write_text
        original_open = Path.open

        def track_write(self, *args, **kwargs):
            stub_writes.append(self)
            return original_write(self, *args, **kwargs)

        def track_open(self, mode="r", *args, **kwargs):
            if "a" in mode:
                stub_writes.append((self, mode))
            return original_open(self, mode=mode, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", track_write)
        monkeypatch.setattr(Path, "open", track_open)

        class _Suspend:
            def __enter__(self):
                return None
            def __exit__(self, *_args):
                return False

        class _App:
            console = SimpleNamespace(size=SimpleNamespace(width=100))
            def suspend(self):
                return _Suspend()

        monkeypatch.setattr("llm_archive.glow.is_available", lambda: True)
        monkeypatch.setattr("llm_archive.glow.is_too_large", lambda _path: False)
        monkeypatch.setattr("llm_archive.glow.view", lambda _p, **kw: 0)

        _open_thread_pager(cast(Any, _App()), data, xl_db)

        assert md_path.exists(), "stub file was created"
        stub_content = md_path.read_text()
        stub_size = md_path.stat().st_size

        # Stub should be minimal: header + opening screen marker
        assert "<!-- msg:" not in stub_content, "stub should not have message markers yet"
        assert "<!-- thread:" in stub_content, "stub should have thread header"
        assert stub_size < 4096, f"stub should be small, got {stub_size} bytes"

        perf_report.metric("viewer.stub_size", stub_size)
        perf_report.insight(f"stub creation writes {stub_size} bytes without full export")

        # Now do full export - should append messages
        full_path = export.write_thread(xl_db, "perf:xl", "perf", config, force=True)
        full_content = full_path.read_text()
        full_size = full_path.stat().st_size

        perf_report.metric("viewer.full_size", full_size)
        perf_report.metric("viewer.size_ratio", full_size / stub_size)
        perf_report.insight(f"full export is {full_size / stub_size:.1f}x larger than stub")

        assert full_path == md_path, "same path"
        assert full_content.startswith(stub_content), "full content starts with stub"
        assert full_size > stub_size, "full export larger than stub"
        assert "<!-- msg:" in full_content, "full export has messages"

    def test_concurrent_exports_with_file_lock(self, tmp_path, perf_con, perf_report):
        """Prove file lock prevents concurrent export conflicts."""
        from llm_archive import export, db
        from llm_archive.schema import IngestedThread, IngestedMessage, IngestedPart
        from concurrent.futures import ThreadPoolExecutor, as_completed

        config = SimpleNamespace(
            export=SimpleNamespace(dir=str(tmp_path / "exports"), auto=True)
        )

        thread_id = "concurrent:lock"
        base_thread = IngestedThread(
            id=thread_id,
            source_id="perf",
            title="Concurrent lock test",
            created_at=1700000000000,
            updated_at=1700000100000,
            messages=[
                IngestedMessage(
                    id=i,
                    thread_id=thread_id,
                    role="user",
                    created_at=1700000000000 + i * 1000,
                    content=f"base message {i}",
                    parts=[IngestedPart(kind="text", text=f"base chunk {i}")],
                )
                for i in range(10)
            ],
        )
        db.save_thread(perf_con, base_thread)

        db_path = tmp_path / "perf.db"

        def export_worker(worker_id):
            con = db.connect(db_path)
            try:
                return export.write_thread(con, thread_id, "perf", config, force=True)
            finally:
                con.close()

        # Run concurrent exports
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(export_worker, i) for i in range(10)]
            results = [f.result() for f in as_completed(futures)]
        elapsed = time.perf_counter() - t0

        perf_report.metric("lock.concurrent_exports_time", elapsed, workers=10)

        # All should succeed and return same path
        assert len(results) == 10, "all exports completed"
        assert all(r == results[0] for r in results), "all returned same path"

        md_path = results[0]
        assert md_path.exists(), "file exists"
        content = md_path.read_text()

        # Verify no corruption: exactly 10 message markers
        msg_count = content.count("<!-- msg:")
        perf_report.metric("lock.final_message_count", msg_count)

        assert msg_count == 10, f"expected 10 messages, got {msg_count}"
        assert "<!-- thread:" in content, "has thread header"
        assert "base chunk 0" in content, "has first message"

        # Verify file is not corrupted (valid markdown structure)
        lines_content = content.split("\n")
        assert lines_content[0].startswith("<!-- thread:"), "starts with thread header"
        assert any(line.startswith("# ") for line in lines_content[:10]), "has title within first 10 lines"

        perf_report.insight(f"concurrent exports completed in {elapsed:.4f}s with valid output")
