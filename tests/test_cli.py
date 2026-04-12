from __future__ import annotations
from pathlib import Path

import pytest
from click.testing import CliRunner

from llm_archive import cli
from llm_archive.schema import IngestedMessage, IngestedThread


class FakeIngestor:
    source_id = "claudecode"

    def __init__(self):
        self.path = None
        self.init_calls: list[dict] = []

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        self.init_calls.append(kwargs)

    async def threads(self, since: int | None = None):
        if False:
            yield None


class CountIngestor(FakeIngestor):
    async def count_threads(self, since: int | None = None):
        return 2

    async def threads(self, since: int | None = None):
        from llm_archive.schema import IngestedMessage, IngestedThread
        yield IngestedThread(
            id="test:1",
            source_id=self.source_id,
            title="a",
            created_at=0,
            updated_at=1,
            messages=[IngestedMessage(id="m1", thread_id="test:1", role="user", content="a", created_at=0)],
        )
        yield IngestedThread(
            id="test:2",
            source_id=self.source_id,
            title="b",
            created_at=0,
            updated_at=1,
            messages=[IngestedMessage(id="m2", thread_id="test:2", role="user", content="b", created_at=0)],
        )


@pytest.mark.asyncio
async def test_sync_runs_init_on_first_sync(monkeypatch, tmp_path):
    ingestor = FakeIngestor()
    saved = []
    last = []

    async def do_ingest(con, ing, since):
        saved.append((ing.source_id, since))
        return True

    monkeypatch.setattr(cli.db, "DB_PATH", tmp_path / "archive.db")
    monkeypatch.setattr(cli, "get_ingestor", lambda source: ingestor)
    monkeypatch.setattr(cli, "_do_ingest", do_ingest)
    monkeypatch.setattr(cli.db, "get_last_sync", lambda con, source: None)
    monkeypatch.setattr(cli.db, "set_last_sync", lambda con, source, ts: last.append((source, ts)))

    await cli._sync("claudecode", None, "/tmp/foo")
    assert ingestor.path == Path("/tmp/foo")
    assert ingestor.init_calls == [{"path": "/tmp/foo"}]
    assert saved == [("claudecode", None)]
    assert len(last) == 1
    assert last[0][0] == "claudecode"


@pytest.mark.asyncio
async def test_sync_skips_init_after_first_sync(monkeypatch, tmp_path):
    ingestor = FakeIngestor()
    saved = []

    async def do_ingest(con, ing, since):
        saved.append((ing.source_id, since))
        return True

    monkeypatch.setattr(cli.db, "DB_PATH", tmp_path / "archive.db")
    monkeypatch.setattr(cli, "get_ingestor", lambda source: ingestor)
    monkeypatch.setattr(cli, "_do_ingest", do_ingest)
    monkeypatch.setattr(cli.db, "get_last_sync", lambda con, source: 1234)
    monkeypatch.setattr(cli.db, "set_last_sync", lambda con, source, ts: None)
    monkeypatch.setattr(cli, "_source_thread_count", lambda con, source: 1)

    await cli._sync("claudecode", None, "/tmp/foo")
    assert ingestor.path == Path("/tmp/foo")
    assert ingestor.init_calls == []
    assert saved == [("claudecode", 1234)]


@pytest.mark.asyncio
async def test_sync_does_not_advance_last_sync_on_failed_ingest(monkeypatch, tmp_path):
    ingestor = FakeIngestor()
    last = []

    async def do_ingest(con, ing, since):
        return False

    monkeypatch.setattr(cli.db, "DB_PATH", tmp_path / "archive.db")
    monkeypatch.setattr(cli, "get_ingestor", lambda source: ingestor)
    monkeypatch.setattr(cli, "_do_ingest", do_ingest)
    monkeypatch.setattr(cli.db, "get_last_sync", lambda con, source: None)
    monkeypatch.setattr(cli.db, "set_last_sync", lambda con, source, ts: last.append((source, ts)))

    await cli._sync("deepseek", None)
    assert ingestor.init_calls == [{"path": None}]
    assert last == []


@pytest.mark.asyncio
async def test_sync_falls_back_to_full_sync_when_last_sync_exists_but_no_threads(monkeypatch, tmp_path):
    ingestor = FakeIngestor()
    ingestor.source_id = "deepseek"
    saved = []

    async def do_ingest(con, ing, since):
        saved.append((ing.source_id, since))
        return True

    monkeypatch.setattr(cli.db, "DB_PATH", tmp_path / "archive.db")
    monkeypatch.setattr(cli, "get_ingestor", lambda source: ingestor)
    monkeypatch.setattr(cli, "_do_ingest", do_ingest)
    monkeypatch.setattr(cli.db, "get_last_sync", lambda con, source: 1234)
    monkeypatch.setattr(cli.db, "set_last_sync", lambda con, source, ts: None)
    monkeypatch.setattr(cli, "_source_thread_count", lambda con, source: 0)

    await cli._sync("deepseek", None)
    assert ingestor.init_calls == [{"path": None}]
    assert saved == [("deepseek", None)]


@pytest.mark.asyncio
async def test_do_ingest_supports_count_threads(tmp_path):
    con = cli.db.connect(tmp_path / "archive.db")
    ok = await cli._do_ingest(con, CountIngestor(), since=None)
    assert ok is True
    assert con.execute("select count(*) from threads").fetchone()[0] == 2


def test_search_command_outputs_grouped_matches(tmp_path):
    con = cli.db.connect(tmp_path / "archive.db")
    cli.db.save_thread(
        con,
        IngestedThread(
            id="claudecode:abc123",
            source_id="claudecode",
            title="Search Title",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(
                    id="claudecode:m1",
                    thread_id="claudecode:abc123",
                    role="user",
                    content="hello search term world " * 20,
                    created_at=1,
                ),
                IngestedMessage(
                    id="claudecode:m2",
                    thread_id="claudecode:abc123",
                    role="assistant",
                    content="another search term hit",
                    created_at=2,
                ),
            ],
        ),
    )
    result = CliRunner().invoke(cli.main, ["search", "search term", "--db-path", str(tmp_path / "archive.db")])
    assert result.exit_code == 0
    assert result.output.count("claudecode:abc123") == 1
    assert "Search Title" in result.output
    assert "…" in result.output
    assert "[1m" not in result.output
    assert "[0m" not in result.output


def test_search_threads_only_shows_counts(tmp_path):
    con = cli.db.connect(tmp_path / "archive.db")
    cli.db.save_thread(
        con,
        IngestedThread(
            id="claude:t1",
            source_id="claude",
            title="Thread",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(id="claude:m1", thread_id="claude:t1", role="user", content="search hit one", created_at=1),
                IngestedMessage(id="claude:m2", thread_id="claude:t1", role="assistant", content="search hit two", created_at=2),
            ],
        ),
    )
    result = CliRunner().invoke(cli.main, ["search", "-t", "search", "--db-path", str(tmp_path / "archive.db")])
    assert result.exit_code == 0
    assert "claude:t1" in result.output
    assert "2 matching messages" in result.output
    assert "search hit one" not in result.output


def test_search_command_snippet_centers_match(tmp_path):
    con = cli.db.connect(tmp_path / "archive.db")
    cli.db.save_thread(
        con,
        IngestedThread(
            id="claude:t2",
            source_id="claude",
            title="Late match",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(
                    id="claude:m3",
                    thread_id="claude:t2",
                    role="assistant",
                    content=("prefix " * 80) + "raspberry" + (" suffix" * 10),
                    created_at=1,
                )
            ],
        ),
    )
    result = CliRunner().invoke(cli.main, ["search", "raspberry", "--db-path", str(tmp_path / "archive.db")])
    assert result.exit_code == 0
    assert "raspberry" in result.output.lower()
    assert "prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix prefix" not in result.output


def test_show_command_prints_full_conversation(tmp_path):
    con = cli.db.connect(tmp_path / "archive.db")
    cli.db.save_thread(
        con,
        IngestedThread(
            id="deepseek:x1",
            source_id="deepseek",
            title="Conversation",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(id="deepseek:m1", thread_id="deepseek:x1", role="user", content="hello", created_at=1),
                IngestedMessage(id="deepseek:m2", thread_id="deepseek:x1", role="assistant", content="world", created_at=2),
            ],
        ),
    )
    result = CliRunner().invoke(cli.main, ["show", "deepseek:x1", "--db-path", str(tmp_path / "archive.db")])
    assert result.exit_code == 0
    assert "deepseek:x1" in result.output
    assert "Conversation" in result.output
    assert "1970-01-01 00:00:00" in result.output
    assert "user" in result.output
    assert "assistant" in result.output
    assert "hello" in result.output
    assert "world" in result.output


def test_show_command_returns_error_when_missing(tmp_path):
    result = CliRunner().invoke(cli.main, ["show", "claude:nope", "--db-path", str(tmp_path / "archive.db")])
    assert result.exit_code == 1
    assert "Thread not found: claude:nope" in result.output


def test_show_command_renders_normalized_parts(tmp_path):
    con = cli.db.connect(tmp_path / "archive.db")
    cli.db.save_thread(
        con,
        IngestedThread(
            id="claudecode:t3",
            source_id="claudecode",
            title="Parts",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(
                    id="claudecode:p1",
                    thread_id="claudecode:t3",
                    role="assistant",
                    content="[Tool: Bash]\nls\n\n[Tool result]\nok",
                    created_at=1,
                )
            ],
        ),
    )
    result = CliRunner().invoke(cli.main, ["show", "claudecode:t3", "--db-path", str(tmp_path / "archive.db")])
    assert result.exit_code == 0
    assert "[Tool: Bash]" in result.output
    assert "ls" in result.output
    assert "[Tool result]" in result.output
    assert "ok" in result.output


def test_print_output_skips_pager_for_short_output(monkeypatch):
    calls = []

    class Pager:
        def __enter__(self):
            calls.append("enter")
            return None

        def __exit__(self, exc_type, exc, tb):
            calls.append("exit")
            return False

    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda fallback: __import__("os").terminal_size((80, 40)))
    monkeypatch.setattr(cli.console, "pager", lambda styles=True: Pager())
    cli._print_lines(["hello", "world"])
    assert calls == []


def test_msg_marker_formats_seconds():
    assert cli._msg_marker(1000) == "1970-01-01 00:00:01"
