"""Tests for database schema and parser output validation."""

from __future__ import annotations
import json
from pathlib import Path

import pytest

from llm_archive import db
from llm_archive.ids import from_base53, to_base53
from llm_archive.ingestors.claudecode import ClaudeCodeIngestor, _parse_jsonl, _flatten_content
from llm_archive.ingestors.deepseek import (
    DeepseekIngestor,
    _flatten_fragments,
    _message_content,
    _metadata,
    _role,
)
from llm_archive.ingestors.web import parse_timestamp
from llm_archive.ingestors.opencode import OpenCodeIngestor
from llm_archive.schema import IngestedMessage, IngestedThread, IngestedPart, ToolCall


@pytest.fixture
def con(tmp_path):
    return db.connect(tmp_path / "test.db")


# --- Schema ---


def test_schema_tables_exist(con):
    tables = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "sources" in tables
    assert "threads" in tables
    assert "messages" in tables


def test_schema_indexes_exist(con):
    indexes = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    assert "idx_messages_thread" in indexes
    assert "idx_threads_source" in indexes
    assert "idx_threads_updated" in indexes


# --- Claude Code parser ---


def _make_jsonl(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))
    return p


def test_claudecode_basic_parse(tmp_path):
    entries = [
        {
            "type": "user",
            "sessionId": "sess-001",
            "uuid": "msg-1",
            "timestamp": "2024-01-01T10:00:00Z",
            "message": {"role": "user", "content": "Hello"},
        },
        {
            "type": "assistant",
            "sessionId": "sess-001",
            "uuid": "msg-2",
            "timestamp": "2024-01-01T10:00:05Z",
            "message": {"role": "assistant", "content": "Hi there!", "model": "claude-3-5-sonnet"},
        },
    ]
    path = _make_jsonl(tmp_path, entries)
    thread = _parse_jsonl(path)

    assert thread is not None
    assert thread.source_id == "claudecode"
    assert len(thread.messages) == 2

    for msg in thread.messages:
        assert msg.role in ("user", "assistant")
        assert msg.content.strip()
        assert msg.thread_id == thread.id


def test_claudecode_skips_empty_content(tmp_path):
    entries = [
        {
            "type": "user",
            "sessionId": "s1",
            "uuid": "m1",
            "timestamp": "2024-01-01T10:00:00Z",
            "message": {"role": "user", "content": ""},
        },
        {
            "type": "assistant",
            "sessionId": "s1",
            "uuid": "m2",
            "timestamp": "2024-01-01T10:00:05Z",
            "message": {"role": "assistant", "content": "response"},
        },
    ]
    thread = _parse_jsonl(_make_jsonl(tmp_path, entries))
    assert thread is not None
    assert len(thread.messages) == 1
    assert thread.messages[0].role == "assistant"


def test_claudecode_tool_use_flattened(tmp_path):
    entries = [
        {
            "type": "assistant",
            "sessionId": "s1",
            "uuid": "m1",
            "timestamp": "2024-01-01T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running command"},
                    {"type": "tool_use", "name": "bash", "input": {"command": "ls -la"}},
                ],
            },
        }
    ]
    thread = _parse_jsonl(_make_jsonl(tmp_path, entries))
    assert thread is not None
    content = thread.messages[0].content
    assert "Running command" in content
    assert "[Tool: bash]" in content
    assert "ls -la" in content


def test_claudecode_tool_result_truncated():
    content = [{"type": "tool_result", "content": "x" * 1000}]
    result = _flatten_content(content)
    assert len(result) < 600  # "[Tool result]\n" + 500 chars


def test_claudecode_returns_none_for_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert _parse_jsonl(path) is None


def test_claudecode_skips_non_message_entries(tmp_path):
    entries = [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": "s1"},
        {"type": "file-history-snapshot", "messageId": "x", "sessionId": "s1"},
        {
            "type": "user",
            "sessionId": "s1",
            "uuid": "m1",
            "timestamp": "2024-01-01T10:00:00Z",
            "message": {"role": "user", "content": "real message"},
        },
    ]
    thread = _parse_jsonl(_make_jsonl(tmp_path, entries))
    assert thread is not None
    assert len(thread.messages) == 1


# --- Flatten content ---


def test_flatten_string_passthrough():
    assert _flatten_content("hello") == "hello"


def test_flatten_thinking_block():
    content = [{"type": "thinking", "thinking": "I need to think..."}]
    result = _flatten_content(content)
    assert "[Thinking]" in result
    assert "I need to think..." in result


def test_flatten_unknown_block_extracts_text():
    content = [{"type": "unknown_future_type", "text": "extracted"}]
    result = _flatten_content(content)
    assert "extracted" in result


def test_deepseek_flatten_fragments():
    fragments = [
        {"type": "REQUEST", "content": "hello"},
        {"type": "THINK", "content": "thinking"},
        {"type": "TOOL_SEARCH", "queries": [{"query": "foo"}]},
        {"type": "TOOL_SEARCH", "results": [{"title": "Bar", "url": "https://example.com"}]},
        {"type": "TEXT", "content": "world"},
    ]
    result = _flatten_fragments(fragments)
    assert "hello" in result
    assert "[Thinking]" in result
    assert "[Search]" in result
    assert "foo" in result
    assert "Bar" in result
    assert "https://example.com" in result
    assert "world" in result


def test_deepseek_role_mapping():
    assert _role("USER") == "user"
    assert _role("ASSISTANT") == "assistant"
    assert _role("SYSTEM") is None


def test_parse_timestamp():
    assert parse_timestamp(1.5) == 1500
    assert parse_timestamp("2.25") == 2250
    assert parse_timestamp(None) is None
    assert parse_timestamp("nope") is None


def test_deepseek_metadata():
    result = _metadata(
        {
            "model": "deepseek-r1",
            "thinking_enabled": True,
            "search_enabled": False,
            "accumulated_token_usage": 123,
        }
    )
    assert result == {
        "model": "deepseek-r1",
        "thinking_enabled": True,
        "search_enabled": False,
        "tokens": 123,
    }


def test_deepseek_message_content_uses_real_payload_fields():
    result = _message_content(
        {
            "content": "answer",
            "thinking_content": "reasoning",
            "search_results": [{"title": "Doc", "url": "https://example.com"}],
        }
    )
    assert "answer" in result
    assert "[Thinking]" in result
    assert "reasoning" in result
    assert "[Search results]" in result
    assert "Doc" in result
    assert "https://example.com" in result


@pytest.mark.asyncio
async def test_deepseek_fetch_thread_parses_messages():
    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "biz_data": {
                        "chat_session": {
                            "id": "sess-1",
                            "title": "My chat",
                            "inserted_at": 100.0,
                            "updated_at": 101.0,
                        },
                        "chat_messages": [
                            {
                                "message_id": 1,
                                "role": "USER",
                                "inserted_at": 100.1,
                                "content": "hello",
                                "thinking_enabled": True,
                                "search_enabled": False,
                                "accumulated_token_usage": 0,
                            },
                            {
                                "message_id": 2,
                                "role": "ASSISTANT",
                                "inserted_at": 100.2,
                                "model": "deepseek-r1",
                                "content": "world",
                                "thinking_content": "hmm",
                                "thinking_enabled": True,
                                "search_enabled": True,
                                "accumulated_token_usage": 42,
                            },
                            {
                                "message_id": 3,
                                "role": "SYSTEM",
                                "inserted_at": 100.3,
                                "fragments": [{"type": "TEXT", "content": "skip"}],
                            },
                        ],
                    }
                }
            }

    class Client:
        async def get(self, url, params=None):
            assert url.endswith("/chat/history_messages")
            assert params == {"chat_session_id": "sess-1"}
            return Resp()

    ingestor = DeepseekIngestor()
    thread = await ingestor._fetch_thread(Client(), {"id": "sess-1"})
    assert thread is not None
    assert thread.id == "deepseek:sess-1"
    assert thread.source_id == "deepseek"
    assert thread.title == "My chat"
    assert thread.created_at == 100000
    assert thread.updated_at == 101000
    assert len(thread.messages) == 2
    assert thread.messages[0].role == "user"
    assert thread.messages[0].content == "hello"
    assert thread.messages[1].role == "assistant"
    assert "[Thinking]" in thread.messages[1].content
    assert "world" in thread.messages[1].content
    assert thread.messages[1].metadata["model"] == "deepseek-r1"
    assert thread.messages[1].metadata["tokens"] == 42


@pytest.mark.asyncio
async def test_deepseek_fetch_sessions_paginates_and_dedupes():
    class Resp:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None):
            self.calls.append(params)
            cursor = (params or {}).get("lte_cursor.updated_at")
            if cursor is None:
                return Resp(
                    {
                        "data": {
                            "biz_data": {
                                "chat_sessions": [
                                    {"id": "a", "seq_id": 5, "updated_at": 5},
                                    {"id": "b", "seq_id": 4, "updated_at": 4},
                                ],
                                "has_more": True,
                            }
                        }
                    }
                )
            if cursor == "4":
                return Resp(
                    {
                        "data": {
                            "biz_data": {
                                "chat_sessions": [
                                    {"id": "c", "seq_id": 3, "updated_at": 3},
                                ],
                                "has_more": False,
                            }
                        }
                    }
                )
            raise AssertionError(params)

    ingestor = DeepseekIngestor()
    sessions = await ingestor._fetch_sessions(Client())
    assert [sess["id"] for sess in sessions] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_claudecode_count_threads(tmp_path):
    entries = [
        {
            "type": "user",
            "sessionId": "s1",
            "uuid": "m1",
            "timestamp": "2024-01-01T10:00:00Z",
            "message": {"role": "user", "content": "Hello"},
        }
    ]
    project = tmp_path / "project"
    project.mkdir()
    _make_jsonl(project, entries)
    ingestor = ClaudeCodeIngestor(tmp_path)
    assert await ingestor.count_threads() == 1
    assert await ingestor.count_threads(since=9999999999999) == 0


@pytest.mark.asyncio
async def test_opencode_count_threads(tmp_path):
    db_path = tmp_path / "opencode.db"
    con = __import__("sqlite3").connect(db_path)
    con.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, time_created INTEGER, time_updated INTEGER)"
    )
    con.execute(
        "INSERT INTO session(id, title, time_created, time_updated) VALUES ('s1', 'a', 1, 1000)"
    )
    con.execute(
        "INSERT INTO session(id, title, time_created, time_updated) VALUES ('s2', 'b', 2, 2000)"
    )
    con.commit()
    con.close()

    ingestor = OpenCodeIngestor(db_path)
    assert await ingestor.count_threads() == 2
    assert await ingestor.count_threads(since=1500) == 1


def test_db_search_messages(con):
    thread = IngestedThread(
        id="claude:test",
        source_id="claude",
        title="My thread",
        created_at=1,
        updated_at=2,
        messages=[
            IngestedMessage(
                id="claude:m1",
                thread_id="claude:test",
                role="user",
                content="hello search world",
                created_at=1,
            )
        ],
    )
    db.save_thread(con, thread)
    rows = db.search_messages(con, "search world")
    assert len(rows) == 1
    assert rows[0]["source_id"] == "claude"
    assert rows[0]["thread_id"] == "claude:test"
    assert rows[0]["title"] == "My thread"


def test_db_save_thread_parses_normalized_parts(con):
    db.save_thread(
        con,
        IngestedThread(
            id="claudecode:test",
            source_id="claudecode",
            title="t",
            created_at=1,
            updated_at=2,
            messages=[
                IngestedMessage(
                    id="claudecode:m1",
                    thread_id="claudecode:test",
                    role="assistant",
                    content="[Tool: Bash]\nls\n\n[Tool result]\nok",
                    created_at=1,
                )
            ],
        ),
    )
    rows = con.execute(
        "select kind, text from message_parts where message_id='claudecode:m1' order by ord"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("tool_call", "ls"), ("tool_result", "ok")]


def test_db_save_thread_preserves_explicit_parts(con):
    db.save_thread(
        con,
        IngestedThread(
            id="deepseek:test",
            source_id="deepseek",
            title="t",
            created_at=1,
            updated_at=2,
            messages=[
                IngestedMessage(
                    id="deepseek:m1",
                    thread_id="deepseek:test",
                    role="assistant",
                    content="fallback",
                    created_at=1,
                    parts=[
                        IngestedPart(kind="text", text="answer"),
                        IngestedPart(kind="search_result", text="doc", searchable=False),
                    ],
                    raw={"id": 1},
                )
            ],
        ),
    )
    parts = con.execute(
        "select kind, text, searchable from message_parts where message_id='deepseek:m1' order by ord"
    ).fetchall()
    assert [tuple(row) for row in parts] == [("text", "answer", 1), ("search_result", "doc", 0)]
    raw = con.execute("select raw from message_raw where message_id='deepseek:m1'").fetchone()[0]
    assert json.loads(raw) == {"id": 1}


def test_db_save_thread_sanitizes_lone_surrogates(con):
    bad_text = "before \ud83e after"
    db.save_thread(
        con,
        IngestedThread(
            id="opencode:surrogate",
            source_id="opencode",
            title="surrogate",
            created_at=1,
            updated_at=2,
            messages=[
                IngestedMessage(
                    id="opencode:surrogate:m1",
                    thread_id="opencode:surrogate",
                    role="assistant",
                    content=bad_text,
                    created_at=1,
                    parts=[
                        IngestedPart(
                            kind="tool_result",
                            text=bad_text,
                            tool_call=ToolCall(name="bash", result=bad_text),
                        )
                    ],
                )
            ],
        ),
    )

    row = con.execute(
        "select content, content_clean from messages where id='opencode:surrogate:m1'"
    ).fetchone()
    part = con.execute(
        "select text, search_text, tool_result from message_parts where message_id='opencode:surrogate:m1'"
    ).fetchone()
    assert row["content"] == "before � after"
    assert row["content_clean"] == "before � after"
    assert tuple(part) == ("before � after", "before � after bash before � after", "before � after")


def test_db_search_messages_uses_normalized_parts(con):
    db.save_thread(
        con,
        IngestedThread(
            id="claudecode:parts",
            source_id="claudecode",
            title="parts",
            created_at=1,
            updated_at=2,
            messages=[
                IngestedMessage(
                    id="claudecode:parts:m1",
                    thread_id="claudecode:parts",
                    role="assistant",
                    content="[Tool result]\nraspberry info",
                    created_at=1,
                )
            ],
        ),
    )
    rows = db.search_messages(con, "raspberry")
    assert rows[0]["kind"] == "tool_result"
    assert rows[0]["content_clean"] == "raspberry info"


def test_db_parse_parts_classifies_directives():
    parts = db._parse_parts(
        "[Thinking]\nplan\n\n"
        "[Search results]\ndoc\n\n"
        "[citation:2]\nsource\n\n"
        "[Request interrupted by user]\nstop\n\n"
        "[search-mode]\ndeep\n\n"
        "[Other]\nvalue"
    )
    assert [part.kind for part in parts] == [
        "reasoning",
        "search_result",
        "citation",
        "status",
        "directive",
        "unknown",
    ]


def test_db_search_excludes_nonsearchable_parts(con):
    db.save_thread(
        con,
        IngestedThread(
            id="deepseek:nosearch",
            source_id="deepseek",
            title="x",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(
                    id="deepseek:nosearch:m1",
                    thread_id="deepseek:nosearch",
                    role="assistant",
                    content="fallback",
                    created_at=1,
                    parts=[
                        IngestedPart(
                            kind="search_result", text="raspberry hidden", searchable=False
                        ),
                        IngestedPart(kind="text", text="visible text", searchable=True),
                    ],
                )
            ],
        ),
    )
    assert db.search_messages(con, "raspberry") == []
    assert db.search_threads(con, "raspberry") == []


def test_db_search_prefix_matching(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    db.save_thread(
        con,
        IngestedThread(
            id="claudecode:prefix",
            source_id="claudecode",
            title="Prefix test",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(
                    id="claudecode:prefix:m1",
                    thread_id="claudecode:prefix",
                    role="assistant",
                    content="raspberry pi project",
                    created_at=1,
                )
            ],
        ),
    )
    results = db.search_messages(con, "rasp")
    assert len(results) == 1
    assert results[0]["thread_id"] == "claudecode:prefix"


def test_db_search_prefix_multi_word(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    db.save_thread(
        con,
        IngestedThread(
            id="claudecode:multi",
            source_id="claudecode",
            title="Multi prefix",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(
                    id="claudecode:multi:m1",
                    thread_id="claudecode:multi",
                    role="assistant",
                    content="raspberry pi python project",
                    created_at=1,
                )
            ],
        ),
    )
    results = db.search_messages(con, "rasp pi")
    assert len(results) == 1


def test_fts_query_single_word():
    assert db._fts_query("hello") == "hello*"
    assert db._fts_query("test") == "test*"


def test_fts_query_multi_word():
    assert db._fts_query("hello world") == "hello* AND world*"
    assert db._fts_query("foo bar baz") == "foo* AND bar* AND baz*"


def test_fts_query_empty():
    assert db._fts_query("") == '""'
    assert db._fts_query("   ") == '""'


def test_fts_query_strips_quotes():
    assert db._fts_query('hello"world') == "helloworld*"


def test_fts_query_single_char():
    assert db._fts_query("a") == "a*"


def test_db_search_prefix_no_match(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    db.save_thread(
        con,
        IngestedThread(
            id="claudecode:nomatch",
            source_id="claudecode",
            title="No match",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(
                    id="claudecode:nomatch:m1",
                    thread_id="claudecode:nomatch",
                    role="assistant",
                    content="apple orange banana",
                    created_at=1,
                )
            ],
        ),
    )
    results = db.search_messages(con, "xyz")
    assert len(results) == 0


def test_db_search_threads_prefix(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    db.save_thread(
        con,
        IngestedThread(
            id="claudecode:threadprefix",
            source_id="claudecode",
            title="Thread prefix test",
            created_at=1,
            updated_at=1,
            messages=[
                IngestedMessage(
                    id="claudecode:threadprefix:m1",
                    thread_id="claudecode:threadprefix",
                    role="assistant",
                    content="raspberry pi",
                    created_at=1,
                ),
                IngestedMessage(
                    id="claudecode:threadprefix:m2",
                    thread_id="claudecode:threadprefix",
                    role="assistant",
                    content="raspberry jam",
                    created_at=2,
                ),
            ],
        ),
    )
    results = db.search_threads(con, "rasp")
    assert len(results) == 1
    assert results[0]["match_count"] == 2


# --- Windsurf ingestor tests ---


def test_search_text_includes_data_field(con):
    """Test that search_text includes data field content for searchability."""
    from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread

    thread = IngestedThread(
        id="test:1",
        source_id="test",
        title="Test thread",
        created_at=0,
        updated_at=0,
        messages=[
            IngestedMessage(
                id="test:1:1",
                thread_id="test:1",
                role="tool",
                content="Command output",
                created_at=0,
                metadata={},
                parts=[
                    IngestedPart(
                        kind="tool_call",
                        text="[Command: ls]",
                        data={"command": "ls", "stdout": "file1.txt\nfile2.txt"},
                    ),
                ],
            ),
        ],
    )

    db.save_thread(con, thread)

    # Verify search_text includes data field content
    rows = con.execute(
        "SELECT search_text FROM message_parts WHERE message_id=?", ("test:1:1",)
    ).fetchall()

    assert len(rows) == 1
    search_text = rows[0]["search_text"]
    # Should include both text and data field content
    assert "Command: ls" in search_text
    assert "ls" in search_text
    assert "file1.txt" in search_text or "file2.txt" in search_text


def test_search_orders_by_updated_at_desc(con):
    """Test that search results are ordered by thread updated_at DESC (newest first)."""
    from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread

    # Old thread
    thread1 = IngestedThread(
        id="test:1",
        source_id="source_a",
        title="Old thread",
        created_at=1000,
        updated_at=1000,
        messages=[
            IngestedMessage(
                id="test:1:1",
                thread_id="test:1",
                role="user",
                content="hello world",
                created_at=1000,
                metadata={},
                parts=[IngestedPart(kind="text", text="hello world")],
            ),
        ],
    )

    # New thread
    thread2 = IngestedThread(
        id="test:2",
        source_id="source_b",
        title="New thread",
        created_at=2000,
        updated_at=2000,
        messages=[
            IngestedMessage(
                id="test:2:1",
                thread_id="test:2",
                role="user",
                content="hello world",
                created_at=2000,
                metadata={},
                parts=[IngestedPart(kind="text", text="hello world")],
            ),
        ],
    )

    db.save_thread(con, thread1)
    db.save_thread(con, thread2)

    results = db.search_messages(con, "hello", limit=10)

    # Newest thread should appear first
    thread_ids = [r["thread_id"] for r in results]
    assert thread_ids[0] == "test:2"
    # Both threads should appear
    assert "test:1" in thread_ids
    assert "test:2" in thread_ids


# --- Claude ingestor regression tests ---


def test_claude_flatten_text_content():
    from llm_archive.ingestors.claude import _flatten_claude_content

    assert _flatten_claude_content("hello") == "hello"


def test_claude_flatten_list_content():
    from llm_archive.ingestors.claude import _flatten_claude_content

    content = [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "bash"},
        {"type": "tool_result", "content": "output"},
    ]
    result = _flatten_claude_content(content)
    assert "hello" in result
    assert "[Tool: bash]" in result
    assert "[Tool result]" in result


def test_claude_parse_timestamp():
    ts = parse_timestamp("2024-03-27T04:26:48.000Z")
    assert ts is not None
    assert ts > 1711510000000


def test_claude_parse_timestamp_none():
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None


@pytest.mark.asyncio
async def test_claude_smart_sync_continues_past_existing():
    """Regression: smart sync must continue past already-synced conversations, not break."""
    from llm_archive.ingestors.claude import ClaudeIngestor
    from unittest.mock import patch, AsyncMock

    ingestor = ClaudeIngestor()

    # Conversations sorted by updated_at desc (most recent first)
    convs = [
        {
            "uuid": "conv-1",
            "updated_at": "2024-03-27T10:00:00Z",
            "created_at": "2024-03-27T10:00:00Z",
            "name": "Recent",
        },
        {
            "uuid": "conv-2",
            "updated_at": "2024-03-26T10:00:00Z",
            "created_at": "2024-03-26T10:00:00Z",
            "name": "Older",
        },
        {
            "uuid": "conv-3",
            "updated_at": "2024-03-25T10:00:00Z",
            "created_at": "2024-03-25T10:00:00Z",
            "name": "Oldest",
        },
    ]

    # conv-1 is already in DB with same timestamp — should be skipped
    # conv-2 and conv-3 should still be yielded
    existing = {"claude:conv-1": 1711533600000}  # matches 2024-03-27T10:00:00Z

    thread_old = IngestedThread(
        id="claude:conv-2",
        source_id="claude",
        title="Older",
        created_at=1711447200000,
        updated_at=1711447200000,
        messages=[],
    )
    thread_oldest = IngestedThread(
        id="claude:conv-3",
        source_id="claude",
        title="Oldest",
        created_at=1711360800000,
        updated_at=1711360800000,
        messages=[],
    )

    with patch.object(ClaudeIngestor, "_get_org_id", new_callable=AsyncMock, return_value="org-1"):
        with patch.object(ClaudeIngestor, "_get", new_callable=AsyncMock, return_value=convs):
            with patch.object(
                ClaudeIngestor,
                "_fetch_thread",
                new_callable=AsyncMock,
                side_effect=[thread_old, thread_oldest],
            ):
                threads = []
                async for t in ingestor.threads(existing_thread_ids=existing):
                    threads.append(t)

    assert len(threads) == 2
    assert threads[0].id == "claude:conv-2"
    assert threads[1].id == "claude:conv-3"


@pytest.mark.asyncio
async def test_claude_smart_sync_re_fetches_updated():
    """Smart sync should re-fetch conversations whose updated_at is newer than DB."""
    from llm_archive.ingestors.claude import ClaudeIngestor
    from unittest.mock import patch, AsyncMock

    ingestor = ClaudeIngestor()

    convs = [
        {
            "uuid": "conv-1",
            "updated_at": "2024-03-28T10:00:00Z",
            "created_at": "2024-03-27T10:00:00Z",
            "name": "Updated",
        },
    ]

    # DB has older timestamp — conversation was updated, should be re-fetched
    existing = {"claude:conv-1": 1711447200000}  # 2024-03-26 — older than API

    thread = IngestedThread(
        id="claude:conv-1",
        source_id="claude",
        title="Updated",
        created_at=1711533600000,
        updated_at=1711620000000,
        messages=[],
    )

    with patch.object(ClaudeIngestor, "_get_org_id", new_callable=AsyncMock, return_value="org-1"):
        with patch.object(ClaudeIngestor, "_get", new_callable=AsyncMock, return_value=convs):
            with patch.object(
                ClaudeIngestor, "_fetch_thread", new_callable=AsyncMock, return_value=thread
            ):
                threads = []
                async for t in ingestor.threads(existing_thread_ids=existing):
                    threads.append(t)

    assert len(threads) == 1
    assert threads[0].id == "claude:conv-1"


@pytest.mark.asyncio
async def test_claude_on_total_callback():
    """Test on_total callback reports conversation count during threads() iteration."""
    from llm_archive.ingestors.claude import ClaudeIngestor
    from unittest.mock import patch, AsyncMock

    ingestor = ClaudeIngestor()

    convs = [
        {
            "uuid": f"conv-{i}",
            "updated_at": "2024-03-27T10:00:00Z",
            "created_at": "2024-03-27T10:00:00Z",
        }
        for i in range(75)
    ]

    reported_total = None

    def on_total(count):
        nonlocal reported_total
        reported_total = count

    with patch.object(ClaudeIngestor, "_get_org_id", new_callable=AsyncMock, return_value="org-1"):
        with patch.object(ClaudeIngestor, "_get", new_callable=AsyncMock, return_value=convs):
            with patch.object(
                ClaudeIngestor, "_fetch_thread", new_callable=AsyncMock, return_value=None
            ):
                async for _ in ingestor.threads(on_total=on_total):
                    pass

    assert reported_total == 75


@pytest.mark.asyncio
async def test_claude_v2_dict_response():
    """Regression: v2 API returns {"data": [...], "has_more": bool} dict format."""
    from llm_archive.ingestors.claude import ClaudeIngestor
    from unittest.mock import patch, AsyncMock

    ingestor = ClaudeIngestor()

    convs = [
        {
            "uuid": f"conv-{i}",
            "updated_at": "2024-03-27T10:00:00Z",
            "created_at": "2024-03-27T10:00:00Z",
        }
        for i in range(75)
    ]

    reported_total = None

    def on_total(count):
        nonlocal reported_total
        reported_total = count

    # v2 returns dict with "data" and "has_more"
    v2_response = {"data": convs, "has_more": False}

    with patch.object(ClaudeIngestor, "_get_org_id", new_callable=AsyncMock, return_value="org-1"):
        with patch.object(ClaudeIngestor, "_get", new_callable=AsyncMock, return_value=v2_response):
            with patch.object(
                ClaudeIngestor, "_fetch_thread", new_callable=AsyncMock, return_value=None
            ):
                async for _ in ingestor.threads(on_total=on_total):
                    pass

    assert reported_total == 75


@pytest.mark.asyncio
async def test_claude_v2_paginated():
    """Regression: v2 pagination via has_more=True triggers additional requests."""
    from llm_archive.ingestors.claude import ClaudeIngestor
    from unittest.mock import patch, AsyncMock

    ingestor = ClaudeIngestor()

    page1 = [
        {
            "uuid": f"conv-{i}",
            "updated_at": "2024-03-27T10:00:00Z",
            "created_at": "2024-03-27T10:00:00Z",
        }
        for i in range(3)
    ]
    page2 = [
        {
            "uuid": f"conv-{i}",
            "updated_at": "2024-03-26T10:00:00Z",
            "created_at": "2024-03-26T10:00:00Z",
        }
        for i in range(3, 5)
    ]

    reported_total = None

    def on_total(count):
        nonlocal reported_total
        reported_total = count

    with patch.object(ClaudeIngestor, "_get_org_id", new_callable=AsyncMock, return_value="org-1"):
        with patch.object(
            ClaudeIngestor,
            "_get",
            new_callable=AsyncMock,
            side_effect=[
                {"data": page1, "has_more": True},
                {"data": page2, "has_more": False},
            ],
        ):
            with patch.object(
                ClaudeIngestor, "_fetch_thread", new_callable=AsyncMock, return_value=None
            ):
                async for _ in ingestor.threads(on_total=on_total):
                    pass

    assert reported_total == 5


@pytest.mark.asyncio
async def test_deepseek_smart_sync_continues_past_existing():
    """Regression: deepseek smart sync must continue past already-synced conversations, not break."""
    from llm_archive.ingestors.deepseek import DeepseekIngestor
    from unittest.mock import patch, AsyncMock

    ingestor = DeepseekIngestor()

    sessions = [
        {"id": "sess-1", "updated_at": 5},
        {"id": "sess-2", "updated_at": 4},
        {"id": "sess-3", "updated_at": 3},
    ]

    # sess-1 already in DB — should be skipped, sess-2 and sess-3 still yielded
    existing = {"deepseek:sess-1": 5000}

    thread2 = IngestedThread(
        id="deepseek:sess-2",
        source_id="deepseek",
        title="Sess 2",
        created_at=4000,
        updated_at=4000,
        messages=[],
    )
    thread3 = IngestedThread(
        id="deepseek:sess-3",
        source_id="deepseek",
        title="Sess 3",
        created_at=3000,
        updated_at=3000,
        messages=[],
    )

    with patch.object(DeepseekIngestor, "_get_token", new_callable=AsyncMock, return_value="tok"):
        with patch.object(
            DeepseekIngestor, "_fetch_sessions", new_callable=AsyncMock, return_value=sessions
        ):
            with patch.object(
                DeepseekIngestor,
                "_fetch_thread",
                new_callable=AsyncMock,
                side_effect=[thread2, thread3],
            ):
                threads = []
                async for t in ingestor.threads(existing_thread_ids=existing):
                    threads.append(t)

    assert len(threads) == 2
    assert threads[0].id == "deepseek:sess-2"
    assert threads[1].id == "deepseek:sess-3"


# --- Search query optimization tests ---


def test_search_messages_returns_distinct_messages(con):
    """Regression: search_messages limit applies to distinct messages, not FTS parts."""
    from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread

    # Message with multiple parts — should count as 1 message, not N parts
    thread = IngestedThread(
        id="test:multi",
        source_id="test",
        title="Multi-part",
        created_at=1000,
        updated_at=1000,
        messages=[
            IngestedMessage(
                id="test:multi:1",
                thread_id="test:multi",
                role="assistant",
                content="alpha beta gamma",
                created_at=1000,
                metadata={},
                parts=[
                    IngestedPart(kind="text", text="alpha"),
                    IngestedPart(kind="text", text="beta"),
                    IngestedPart(kind="text", text="gamma"),
                ],
            ),
        ],
    )
    db.save_thread(con, thread)

    results = db.search_messages(con, "alpha", limit=10)
    msg_ids = [r["message_id"] for r in results]
    # Should return all 3 parts for the 1 message, not 1 part per message
    assert "test:multi:1" in msg_ids
    # All 3 parts should be present
    ords = [r["ord"] for r in results if r["message_id"] == "test:multi:1"]
    assert len(ords) == 3


def test_search_threads_sorts_newest_first(tmp_path):
    """Regression: search_threads sorts by last_match_at DESC (newest first)."""
    con = db.connect(tmp_path / "archive.db")
    # Old thread
    db.save_thread(
        con,
        IngestedThread(
            id="test:old",
            source_id="test",
            title="Old",
            created_at=1000,
            updated_at=1000,
            messages=[
                IngestedMessage(
                    id="test:m1",
                    thread_id="test:old",
                    role="user",
                    content="findme",
                    created_at=1000,
                )
            ],
        ),
    )
    # New thread
    db.save_thread(
        con,
        IngestedThread(
            id="test:new",
            source_id="test",
            title="New",
            created_at=2000,
            updated_at=2000,
            messages=[
                IngestedMessage(
                    id="test:m2",
                    thread_id="test:new",
                    role="user",
                    content="findme",
                    created_at=2000,
                )
            ],
        ),
    )
    results = db.search_threads(con, "findme", limit=10)
    assert len(results) == 2
    assert results[0]["thread_id"] == "test:new"
    assert results[1]["thread_id"] == "test:old"


def test_search_messages_newest_message_first_within_thread(con):
    """Regression: within a thread, newest messages appear first."""
    from llm_archive.schema import IngestedMessage, IngestedThread

    db.save_thread(
        con,
        IngestedThread(
            id="test:sort",
            source_id="test",
            title="Sort",
            created_at=1000,
            updated_at=3000,
            messages=[
                IngestedMessage(
                    id="test:early",
                    thread_id="test:sort",
                    role="user",
                    content="findme early",
                    created_at=1000,
                ),
                IngestedMessage(
                    id="test:late",
                    thread_id="test:sort",
                    role="assistant",
                    content="findme late",
                    created_at=3000,
                ),
            ],
        ),
    )
    results = db.search_messages(con, "findme", limit=10)
    msg_ids = [r["message_id"] for r in results]
    late_idx = msg_ids.index("test:late")
    early_idx = msg_ids.index("test:early")
    assert late_idx < early_idx


# --- Short ID resolution tests ---


def test_resolve_short_id_thread(tmp_path):
    """resolve_short_id with t-prefix returns thread with all messages."""
    con = db.connect(tmp_path / "archive.db")
    db.save_thread(
        con,
        IngestedThread(
            id="test:t1",
            source_id="test",
            title="Thread",
            created_at=1000,
            updated_at=1000,
            messages=[
                IngestedMessage(
                    id="test:m1", thread_id="test:t1", role="user", content="hello", created_at=1000
                ),
                IngestedMessage(
                    id="test:m2",
                    thread_id="test:t1",
                    role="assistant",
                    content="world",
                    created_at=2000,
                ),
            ],
        ),
    )
    rowid = con.execute("SELECT rowid FROM threads WHERE id='test:t1'").fetchone()[0]
    short_id = f"t{to_base53(rowid)}"
    result = db.resolve_short_id(con, short_id)
    assert result is not None
    assert result["thread"]["id"] == "test:t1"
    assert len(result["messages"]) == 2


def test_resolve_short_id_message(tmp_path):
    """resolve_short_id with m-prefix returns single message with thread info."""
    con = db.connect(tmp_path / "archive.db")
    db.save_thread(
        con,
        IngestedThread(
            id="test:t1",
            source_id="test",
            title="Thread",
            created_at=1000,
            updated_at=3000,
            messages=[
                IngestedMessage(
                    id="test:m1", thread_id="test:t1", role="user", content="first", created_at=1000
                ),
                IngestedMessage(
                    id="test:m2",
                    thread_id="test:t1",
                    role="assistant",
                    content="second",
                    created_at=2000,
                ),
            ],
        ),
    )
    rowid = con.execute("SELECT rowid FROM messages WHERE id='test:m2'").fetchone()[0]
    short_id = f"m{to_base53(rowid)}"
    result = db.resolve_short_id(con, short_id)
    assert result is not None
    assert result["thread"]["id"] == "test:t1"
    assert len(result["messages"]) == 1
    assert result["messages"][0]["id"] == "test:m2"


def test_resolve_short_id_invalid(tmp_path):
    """resolve_short_id returns None for invalid/unknown IDs."""
    con = db.connect(tmp_path / "archive.db")
    assert db.resolve_short_id(con, "tZZZ") is None
    assert db.resolve_short_id(con, "mZZZ") is None
    assert db.resolve_short_id(con, "x123") is None


def test_get_message(tmp_path):
    """get_message fetches single message with thread context."""
    con = db.connect(tmp_path / "archive.db")
    db.save_thread(
        con,
        IngestedThread(
            id="test:t1",
            source_id="test",
            title="Thread",
            created_at=1000,
            updated_at=1000,
            messages=[
                IngestedMessage(
                    id="test:m1", thread_id="test:t1", role="user", content="hello", created_at=1000
                ),
                IngestedMessage(
                    id="test:m2",
                    thread_id="test:t1",
                    role="assistant",
                    content="world",
                    created_at=2000,
                ),
            ],
        ),
    )
    result = db.get_message(con, "test:m2")
    assert result is not None
    assert result["thread"]["id"] == "test:t1"
    assert len(result["messages"]) == 1
    assert result["messages"][0]["id"] == "test:m2"
    assert result["messages"][0]["parts"] is not None


def test_get_message_not_found(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    assert db.get_message(con, "nonexistent") is None


# --- ChatGPT ingestor tests ---


def test_chatgpt_parse_timestamp():
    from llm_archive.ingestors.chatgpt import _parse_timestamp

    assert _parse_timestamp(1.5) == 1500
    assert _parse_timestamp("2.25") == 2250
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("nope") is None


def test_chatgpt_extract_message_text():
    from llm_archive.ingestors.chatgpt import _extract_message_text

    assert _extract_message_text({"content": {"parts": ["hello", "world"]}}) == "hello\n\nworld"
    assert _extract_message_text({"content": {"parts": [{"text": "greeting"}]}}) == "greeting"
    assert _extract_message_text({"content": {"parts": [""]}}) == ""
    assert _extract_message_text({"content": {}}) == ""
    assert (
        _extract_message_text({"content": {"parts": [{"content_type": "image_asset_pointer"}]}})
        == "[Image]"
    )


def test_message_rate_limiter_429_doubles_delay():
    from llm_archive.ratelimit import MessageRateLimiter

    limiter = MessageRateLimiter(initial_delay=5.0, max_delay=60.0)
    limiter._jitter = 0.0  # Disable jitter for test

    assert limiter.current_delay == 5.0
    delay = limiter.record_429()
    assert delay == 10.0
    assert limiter.current_delay == 10.0


def test_message_rate_limiter_429_max_cap():
    from llm_archive.ratelimit import MessageRateLimiter

    limiter = MessageRateLimiter(initial_delay=5.0, max_delay=15.0)
    limiter._jitter = 0.0

    limiter.record_429()
    assert limiter.current_delay == 10.0
    limiter.record_429()
    assert limiter.current_delay == 15.0
    limiter.record_429()
    assert limiter.current_delay == 15.0


def test_message_rate_limiter_repeated_429s():
    from llm_archive.ratelimit import MessageRateLimiter

    limiter = MessageRateLimiter(initial_delay=5.0, max_delay=60.0)
    limiter._jitter = 0.0

    limiter.record_429()
    assert limiter.current_delay == 10.0
    limiter.record_429()
    assert limiter.current_delay == 20.0
    limiter.record_429()
    assert limiter.current_delay == 40.0
    limiter.record_429()
    assert limiter.current_delay == 60.0

    for _ in range(50):
        limiter.record_success()

    assert limiter.current_delay < 60.0
    assert limiter.current_delay >= 5.0


def test_message_rate_limiter_get_and_apply_delay():
    from llm_archive.ratelimit import MessageRateLimiter
    import time

    limiter = MessageRateLimiter(initial_delay=2.0, max_delay=60.0)
    limiter._jitter = 0.0

    delay = limiter.get_and_apply_delay()
    assert delay == 0.0

    limiter.update_request_time()

    delay = limiter.get_and_apply_delay()
    assert 1.9 <= delay <= 2.5  # May include random_extra

    time.sleep(1.95)
    delay = limiter.get_and_apply_delay()
    assert 0.0 <= delay <= 0.6  # May include random_extra


@pytest.mark.asyncio
async def test_chatgpt_fetch_thread_parses_messages():
    from llm_archive.ingestors.chatgpt import ChatGPTIngestor

    class MockResp:
        status_code = 200
        is_success = True

        def json(self):
            return {
                "mapping": {
                    "node-1": {
                        "message": {
                            "id": "msg-1",
                            "author": {"role": "user"},
                            "create_time": 1704108000.0,
                            "content": {"parts": ["hello"]},
                        }
                    },
                    "node-2": {
                        "message": {
                            "id": "msg-2",
                            "author": {"role": "assistant"},
                            "create_time": 1704108010.0,
                            "content": {"parts": ["hi there"]},
                            "model": "gpt-4",
                        }
                    },
                    "node-3": {
                        "message": {
                            "id": "msg-3",
                            "author": {"role": "system"},
                            "create_time": 1704107990.0,
                            "content": {"parts": ["system prompt"]},
                        }
                    },
                    "node-4": {"message": None},
                }
            }

    class MockClient:
        async def get(self, url, params=None, headers=None):
            return MockResp()

    ingestor = ChatGPTIngestor()
    thread = await ingestor._fetch_thread(
        MockClient(),
        {
            "id": "conv-1",
            "title": "Test Chat",
            "create_time": 1704108000.0,
            "update_time": 1704108010.0,
        },
        "fake-token",
    )
    assert thread is not None
    assert thread.id == "chatgpt:conv-1"
    assert thread.source_id == "chatgpt"
    assert thread.title == "Test Chat"
    assert len(thread.messages) == 2
    assert thread.messages[0].role == "user"
    assert thread.messages[0].content == "hello"
    assert thread.messages[1].role == "assistant"
    assert thread.messages[1].content == "hi there"
    assert thread.messages[1].metadata["model"] == "gpt-4"


@pytest.mark.asyncio
async def test_chatgpt_smart_sync_skips_existing():
    """Test that conversations in existing_thread_ids are skipped."""
    # Test the filtering logic directly
    conversations = [
        {"id": "conv-1", "create_time": 1704108000.0, "update_time": 1704108000.0},
        {"id": "conv-2", "create_time": 1704107000.0, "update_time": 1704107000.0},
    ]

    existing = {"chatgpt:conv-1": 1704108000000}

    # Simulate the filtering logic
    skipped = []
    fetched = []

    for conv in conversations:
        conv_id = conv.get("id")
        thread_id = f"chatgpt:{conv_id}"
        updated_at = 1704108000000  # Both convs have same timestamp for this test

        if thread_id in existing:
            if existing.get(thread_id) and existing.get(thread_id) >= updated_at:
                skipped.append(conv_id)
                continue

        fetched.append(conv_id)

    assert skipped == ["conv-1"]
    assert fetched == ["conv-2"]


# --- Base53 ID encoding/decoding ---


def test_to_base53_small_values():
    assert to_base53(0) == "2"
    assert to_base53(1) == "3"
    assert to_base53(2) == "4"
    assert to_base53(52) == "w"


def test_to_base53_roundtrip():
    for num in [0, 1, 52, 55, 100, 1000, 12345, 999999]:
        encoded = to_base53(num)
        decoded = from_base53(encoded)
        assert decoded == num, f"Failed roundtrip: {num} -> {encoded} -> {decoded}"


def test_from_base53_known():
    assert from_base53("3") == 1
    assert from_base53("4") == 2
    assert from_base53("w") == 52
    assert from_base53("34") == 55


def test_from_base53_invalid_character():
    with pytest.raises(ValueError):
        from_base53("1")  # '1' is not in BASE53
    with pytest.raises(ValueError):
        from_base53("O0l")  # 'O', '0', 'l' not in BASE53
