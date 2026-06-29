"""Performance tests for DB queries, content rendering, search, and TUI startup.

Synthetic data mimics real thread shapes (part counts, text length ranges) from a
production archive. No real conversation data is stored.

Run:    uv run pytest tests/test_perf.py -v -m perf --override-ini='addopts=""'
Profile: uv run pytest tests/test_perf.py -v -m perf -o addopts="" --profile
"""

from __future__ import annotations

import random
import time

import pytest

from llm_archive import db
from llm_archive.tui import _role_separator

pytestmark = pytest.mark.perf

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


def _thread_row(con, tid: str) -> dict:
    row = con.execute(
        "SELECT id, source_id, title, created_at, updated_at FROM threads WHERE id=?",
        (tid,),
    ).fetchone()
    return dict(row)


def _timed(fn, *a, **kw) -> tuple[float, object]:
    t0 = time.perf_counter()
    r = fn(*a, **kw)
    return time.perf_counter() - t0, r


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


# ─── TUI startup + list render ─────────────────────────────────────────────────

class TestTUIStartup:
    async def test_app_startup_and_list(self, mixed_db, monkeypatch, tol):
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
            listview = app.screen.query_one("ListView")
            assert len(listview) > 50

    async def test_list_render_many_threads(self, perf_con, monkeypatch):
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
        async with app.run_test() as pilot:
            await pilot.pause()
            listview = app.screen.query_one("ListView")
            assert len(listview) > 100

    async def test_open_thread_from_list(self, mixed_db, monkeypatch, tol):
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
            assert elapsed < 2.0 * tol, f"open thread {elapsed:.3f}s"

    def test_cli_startup_e2e(self, perf_con, tol):
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
        assert elapsed < 3.0 * tol, f"CLI startup {elapsed:.3f}s"


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