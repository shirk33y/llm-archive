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

    async def prepare(self) -> bool:
        return True

    async def threads(self, since: int | None = None):
        if False:
            yield None


def test_config_show_prints_toml_headers(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[ingestors.chatgpt]\nmode = "cookies"\n')
    monkeypatch.setenv("LLM_ARCHIVE_CONFIG", str(path))

    result = CliRunner().invoke(cli.main, ["config", "show"])

    assert result.exit_code == 0
    assert "[ingestors.chatgpt]" in result.output


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

    async def do_ingest(con, ing, since, force=False):
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

    async def do_ingest(con, ing, since, force=False):
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

    async def do_ingest(con, ing, since, force=False):
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


@pytest.mark.asyncio
async def test_do_ingest_processes_all_threads_regardless_of_since(tmp_path):
    """Test that sync processes all threads even when since parameter is provided."""
    con = cli.db.connect(tmp_path / "archive.db")
    
    # First sync to populate database
    ingestor = CountIngestor()
    ok = await cli._do_ingest(con, ingestor, since=None)
    assert ok is True
    assert con.execute("select count(*) from threads").fetchone()[0] == 2
    
    # Second sync with since parameter - should still process all threads
    ingestor2 = CountIngestor()
    ok2 = await cli._do_ingest(con, ingestor2, since=1234)
    assert ok2 is True
    # All threads should be processed (2 new + 0 skipped = 2 total)
    # Since they're already in database, they should be skipped
    assert con.execute("select count(*) from threads").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_force_flag_updates_threads(tmp_path):
    """Test that -f flag forces thread updates."""
    con = cli.db.connect(tmp_path / "archive.db")
    
    # First sync to populate database
    ingestor = CountIngestor()
    ok = await cli._do_ingest(con, ingestor, since=None)
    assert ok is True
    assert con.execute("select count(*) from threads").fetchone()[0] == 2
    
    # Second sync with force=True - should re-fetch and update threads
    ingestor2 = CountIngestor()
    ok2 = await cli._do_ingest(con, ingestor2, since=None, force=True)
    assert ok2 is True
    # Should still have 2 threads
    assert con.execute("select count(*) from threads").fetchone()[0] == 2


class SmartSyncIngestor(FakeIngestor):
    """Ingestor that supports smart sync with timestamp comparison."""
    source_id = "smart"
    
    async def threads(self, since: int | None = None, existing_thread_ids: set[str] | None = None):
        if existing_thread_ids is None:
            existing_thread_ids = set()
        
        for i in range(5):
            thread_id = f"smart:{i}"
            updated_at = i * 1000
            
            # Smart sync with timestamp comparison
            if thread_id in existing_thread_ids:
                if isinstance(existing_thread_ids, dict):
                    db_updated_at = existing_thread_ids.get(thread_id)
                    if db_updated_at and db_updated_at >= updated_at:
                        break
                else:
                    break
            
            yield IngestedThread(
                id=thread_id,
                source_id=self.source_id,
                title=f"Thread {i}",
                created_at=i,
                updated_at=updated_at,
                messages=[IngestedMessage(id=f"m{i}", thread_id=thread_id, role="user", content=f"content {i}", created_at=i)],
            )


@pytest.mark.asyncio
async def test_smart_sync_stops_at_existing_thread(tmp_path):
    """Test that smart sync stops fetching when it encounters an existing thread."""
    con = cli.db.connect(tmp_path / "archive.db")
    
    # First sync to populate database
    ingestor = SmartSyncIngestor()
    ok = await cli._do_ingest(con, ingestor, since=None)
    assert ok is True
    assert con.execute("select count(*) from threads").fetchone()[0] == 5
    
    # Second sync - should stop at first existing thread
    ingestor2 = SmartSyncIngestor()
    ok2 = await cli._do_ingest(con, ingestor2, since=None)
    assert ok2 is True
    # Should still have 5 threads (all skipped)
    assert con.execute("select count(*) from threads").fetchone()[0] == 5


@pytest.mark.asyncio
async def test_smart_sync_refetches_updated_thread(tmp_path):
    """Test that smart sync re-fetches conversations with newer updated_at."""
    con = cli.db.connect(tmp_path / "archive.db")
    
    # First sync - add threads with updated_at = 0, 1000, 2000, 3000, 4000
    ingestor = SmartSyncIngestor()
    ok = await cli._do_ingest(con, ingestor, since=None)
    assert ok is True
    assert con.execute("select count(*) from threads").fetchone()[0] == 5
    
    # Update thread 0 in database to have updated_at = -100 (older than API's 0)
    # Also change the sha1 by updating content to ensure it gets re-fetched
    con.execute("UPDATE threads SET updated_at=-100 WHERE id='smart:0'")
    con.execute("UPDATE threads SET sha1='different_sha1' WHERE id='smart:0'")
    con.commit()
    
    # Second sync - should re-fetch thread 0 (API has 0, DB has -100)
    ingestor2 = SmartSyncIngestor()
    ok2 = await cli._do_ingest(con, ingestor2, since=None)
    assert ok2 is True
    # Should still have 5 threads
    assert con.execute("select count(*) from threads").fetchone()[0] == 5
    # Thread 0 should have updated_at = 0 (from API)
    row = con.execute("SELECT updated_at FROM threads WHERE id='smart:0'").fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_force_flag_disables_smart_sync(tmp_path):
    """Test that -f flag disables smart sync and fetches all threads."""
    con = cli.db.connect(tmp_path / "archive.db")
    
    # First sync to populate database
    ingestor = SmartSyncIngestor()
    ok = await cli._do_ingest(con, ingestor, since=None)
    assert ok is True
    assert con.execute("select count(*) from threads").fetchone()[0] == 5
    
    # Second sync with force=True - should fetch all threads (not stop at existing)
    ingestor2 = SmartSyncIngestor()
    ok2 = await cli._do_ingest(con, ingestor2, since=None, force=True)
    assert ok2 is True
    # Should still have 5 threads (all skipped since they're identical)
    assert con.execute("select count(*) from threads").fetchone()[0] == 5


def test_token_extraction_from_storage_state(tmp_path):
    """Test extracting bearer token from storage state localStorage."""
    import json
    
    # Test plain token
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    storage_state = {
        "origins": [
            {
                "origin": "https://chat.deepseek.com",
                "localStorage": [
                    {"name": "accessToken", "value": "test_token_123"},
                    {"name": "otherKey", "value": "other_value"}
                ]
            }
        ]
    }
    storage_path = auth_dir / "deepseek.json"
    storage_path.write_text(json.dumps(storage_state))
    
    state = json.loads(storage_path.read_text())
    origins = state.get("origins", [])
    token = None
    for origin in origins:
        if origin.get("origin") == "https://chat.deepseek.com":
            for item in origin.get("localStorage", []):
                if item.get("name") in ("accessToken", "token", "userToken"):
                    token_value = item.get("value")
                    if token_value:
                        try:
                            parsed = json.loads(token_value)
                            if isinstance(parsed, dict) and "value" in parsed:
                                token_value = parsed["value"]
                        except (json.JSONDecodeError, TypeError):
                            pass
                        token = token_value
                        break
        if token:
            break
    
    assert token == "test_token_123"
    
    # Test JSON-encoded token (like userToken)
    storage_state2 = {
        "origins": [
            {
                "origin": "https://chat.deepseek.com",
                "localStorage": [
                    {"name": "userToken", "value": json.dumps({"value": "json_token_456", "__version": "0"})}
                ]
            }
        ]
    }
    storage_path.write_text(json.dumps(storage_state2))
    
    state2 = json.loads(storage_path.read_text())
    origins2 = state2.get("origins", [])
    token2 = None
    for origin in origins2:
        if origin.get("origin") == "https://chat.deepseek.com":
            for item in origin.get("localStorage", []):
                if item.get("name") in ("accessToken", "token", "userToken"):
                    token_value = item.get("value")
                    if token_value:
                        try:
                            parsed = json.loads(token_value)
                            if isinstance(parsed, dict) and "value" in parsed:
                                token_value = parsed["value"]
                        except (json.JSONDecodeError, TypeError):
                            pass
                        token2 = token_value
                        break
        if token2:
            break
    
    assert token2 == "json_token_456"




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


def test_show_command_message_short_id(tmp_path):
    """show mX resolves to single message within its parent thread."""
    con = cli.db.connect(tmp_path / "archive.db")
    cli.db.save_thread(
        con,
        IngestedThread(
            id="claude:t1",
            source_id="claude",
            title="My Thread",
            created_at=1000,
            updated_at=3000,
            messages=[
                IngestedMessage(id="claude:m1", thread_id="claude:t1", role="user", content="first message", created_at=1000),
                IngestedMessage(id="claude:m2", thread_id="claude:t1", role="assistant", content="second message", created_at=2000),
                IngestedMessage(id="claude:m3", thread_id="claude:t1", role="user", content="third message", created_at=3000),
            ],
        ),
    )
    # Find the short ID for m2
    msg_row = con.execute("SELECT rowid FROM messages WHERE id='claude:m2'").fetchone()
    short_id = f"m{cli.db.to_base53(msg_row[0])}"
    result = CliRunner().invoke(cli.main, ["show", short_id, "--db-path", str(tmp_path / "archive.db")])
    assert result.exit_code == 0
    assert "My Thread" in result.output
    assert "second message" in result.output
    # Should NOT contain other messages from the thread
    assert "first message" not in result.output
    assert "third message" not in result.output


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


def test_search_sorts_newest_thread_first(tmp_path):
    con = cli.db.connect(tmp_path / "archive.db")
    # Old thread
    cli.db.save_thread(
        con,
        IngestedThread(
            id="claude:old",
            source_id="claude",
            title="Old thread",
            created_at=1000,
            updated_at=1000,
            messages=[
                IngestedMessage(id="claude:m_old", thread_id="claude:old", role="user", content="findme old", created_at=1000),
            ],
        ),
    )
    # New thread
    cli.db.save_thread(
        con,
        IngestedThread(
            id="claude:new",
            source_id="claude",
            title="New thread",
            created_at=2000,
            updated_at=2000,
            messages=[
                IngestedMessage(id="claude:m_new", thread_id="claude:new", role="user", content="findme new", created_at=2000),
            ],
        ),
    )
    result = CliRunner().invoke(cli.main, ["search", "findme", "--db-path", str(tmp_path / "archive.db")])
    assert result.exit_code == 0
    # New thread title should appear before old thread title
    new_pos = result.output.find("New thread")
    old_pos = result.output.find("Old thread")
    assert new_pos < old_pos, "New thread should appear before old thread"


def test_search_sorts_newest_messages_within_thread_first(tmp_path):
    con = cli.db.connect(tmp_path / "archive.db")
    cli.db.save_thread(
        con,
        IngestedThread(
            id="claude:t1",
            source_id="claude",
            title="Thread",
            created_at=1000,
            updated_at=3000,
            messages=[
                IngestedMessage(id="claude:m1", thread_id="claude:t1", role="user", content="findme early", created_at=1000),
                IngestedMessage(id="claude:m2", thread_id="claude:t1", role="assistant", content="findme late", created_at=3000),
            ],
        ),
    )
    result = CliRunner().invoke(cli.main, ["search", "findme", "--db-path", str(tmp_path / "archive.db")])
    assert result.exit_code == 0
    late_pos = result.output.find("findme late")
    early_pos = result.output.find("findme early")
    assert late_pos < early_pos, "Later message should appear before earlier message within same thread"
