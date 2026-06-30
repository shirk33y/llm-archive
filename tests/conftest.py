import json
import pathlib
import pstats
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from llm_archive import db


def pytest_addoption(parser):
    parser.addoption("--profile", action="store_true", default=False,
                     help="Record cProfile per perf test to .profiles/")
    parser.addoption(
        "--perf-scale",
        choices=("large", "huge"),
        default="large",
        help="Synthetic archive size for perf tests.",
    )
    parser.addoption(
        "--perf-real-thread-id",
        default="",
        help="Optional live read-only thread id used to calibrate perf synthetic data.",
    )


def pytest_configure(config):
    config._profile = config.getoption("--profile")
    config._perf_records = []
    config._perf_insights = []
    config._profile_summaries = []


class PerfReport:
    def __init__(self, config: pytest.Config, nodeid: str) -> None:
        self.config = config
        self.nodeid = nodeid

    def metric(self, name: str, seconds: float, **fields: Any) -> None:
        records = getattr(self.config, "_perf_records")
        records.append(
            {
                "test": self.nodeid,
                "name": name,
                "seconds": seconds,
                **fields,
            }
        )

    def insight(self, text: str, **fields: Any) -> None:
        insights = getattr(self.config, "_perf_insights")
        insights.append(
            {
                "test": self.nodeid,
                "text": text,
                **fields,
            }
        )


@pytest.fixture
def perf_report(request: pytest.FixtureRequest) -> PerfReport:
    return PerfReport(request.config, request.node.nodeid)


def _profile_summary_rows(stats: pstats.Stats, nodeid: str, prof_path: pathlib.Path, txt_path: pathlib.Path):
    ignored = {
        "_callers.py",
        "_hooks.py",
        "_manager.py",
        "base_events.py",
        "fixtures.py",
        "plugin.py",
        "runners.py",
        "runner.py",
        "python.py",
        "skipping.py",
    }
    rows = []
    for func, stat in getattr(stats, "stats").items():
        _primitive_calls, calls, total, cumulative, _callers = stat
        filename, line, name = func
        short_filename = pathlib.Path(filename).name
        if short_filename in ignored:
            continue
        rows.append(
            {
                "test": nodeid,
                "function": f"{short_filename}:{line}:{name}",
                "calls": calls,
                "total": total,
                "cumulative": cumulative,
                "profile": str(prof_path),
                "summary": str(txt_path),
            }
        )
    rows.sort(key=lambda row: row["cumulative"], reverse=True)
    return rows[:5]


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    records = getattr(config, "_perf_records", [])
    insights = getattr(config, "_perf_insights", [])
    if not records and not insights:
        return

    out_dir = pathlib.Path(".profiles")
    out_dir.mkdir(exist_ok=True)
    report_path = out_dir / "perf-report.json"
    report_path.write_text(
        json.dumps({"records": records, "insights": insights}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    terminalreporter.section("perf insights")
    terminalreporter.write_line(f"report: {report_path}")

    if records:
        slowest = sorted(records, key=lambda row: row["seconds"], reverse=True)[:8]
        terminalreporter.write_line("slowest metrics:")
        for row in slowest:
            terminalreporter.write_line(
                f"  {row['seconds']:.4f}s  {row['name']}  {row['test']}"
            )

    if insights:
        terminalreporter.write_line("insights:")
        for row in insights:
            terminalreporter.write_line(f"  - {row['text']}")

    profile_summaries = getattr(config, "_profile_summaries", [])
    if profile_summaries:
        terminalreporter.write_line("profile hotspots:")
        for row in profile_summaries[:8]:
            terminalreporter.write_line(
                f"  {row['cumulative']:.4f}s  {row['function']}  {row['test']}"
            )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if not item.get_closest_marker("perf") or not item.config._profile:
        yield
        return
    import cProfile
    import io

    profiler = cProfile.Profile()
    profiler.enable()
    try:
        yield
    finally:
        profiler.disable()
    prof_dir = pathlib.Path(".profiles")
    prof_dir.mkdir(exist_ok=True)
    name = item.name.replace("[", "_").replace("]", "")
    prof_path = prof_dir / f"{name}.prof"
    txt_path = prof_dir / f"{name}.pstats.txt"
    profiler.dump_stats(str(prof_path))

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative")
    stats.print_stats(30)
    txt_path.write_text(stream.getvalue(), encoding="utf-8")

    rows = _profile_summary_rows(stats, item.nodeid, prof_path, txt_path)
    getattr(item.config, "_profile_summaries").extend(rows)


@pytest.fixture(autouse=True)
def isolate_archive_paths(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    db_path = tmp_path / "archive.db"
    monkeypatch.setenv("LLM_ARCHIVE_CONFIG", str(config_path))
    monkeypatch.setenv("LLM_ARCHIVE_DB", str(db_path))
    monkeypatch.delenv("LLM_ARCHIVE_ENABLE_TEST_SOURCES", raising=False)
    monkeypatch.setattr(db, "DB_PATH", db_path)


@pytest.fixture
def con() -> Iterator[sqlite3.Connection]:
    con = db.connect()
    yield con
    try:
        con.close()
    except sqlite3.ProgrammingError:
        pass


@pytest.fixture(autouse=True)
def close_sqlite_connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def tracked_connect(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        connections.append(con)
        return con

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    yield
    for con in connections:
        try:
            con.close()
        except sqlite3.ProgrammingError:
            pass
