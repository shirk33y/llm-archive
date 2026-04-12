"""Tests for database schema and parser output validation."""
from __future__ import annotations
import json
import textwrap
from pathlib import Path

import pytest

from llm_archive import db
from llm_archive.ingestors.claudecode import ClaudeCodeIngestor, _parse_jsonl, _flatten_content
from llm_archive.ingestors.deepseek import DeepseekIngestor, _flatten_fragments, _message_content, _metadata, _parse_ts, _role
from llm_archive.ingestors.opencode import OpenCodeIngestor, _build_thread
from llm_archive.schema import IngestedMessage, IngestedThread, IngestedPart


@pytest.fixture
def con(tmp_path):
    return db.connect(tmp_path / "test.db")


# --- Schema ---

def test_schema_tables_exist(con):
    tables = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "sources" in tables
    assert "threads" in tables
    assert "messages" in tables


def test_schema_indexes_exist(con):
    indexes = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_messages_thread" in indexes
    assert "idx_threads_source" in indexes
    assert "idx_threads_updated" in indexes


# --- Claude Code parser ---

def _make_jsonl(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))
    return p


def test_claude_code_basic_parse(tmp_path):
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


def test_claude_code_skips_empty_content(tmp_path):
    entries = [
        {"type": "user", "sessionId": "s1", "uuid": "m1",
         "timestamp": "2024-01-01T10:00:00Z",
         "message": {"role": "user", "content": ""}},
        {"type": "assistant", "sessionId": "s1", "uuid": "m2",
         "timestamp": "2024-01-01T10:00:05Z",
         "message": {"role": "assistant", "content": "response"}},
    ]
    thread = _parse_jsonl(_make_jsonl(tmp_path, entries))
    assert thread is not None
    assert len(thread.messages) == 1
    assert thread.messages[0].role == "assistant"


def test_claude_code_tool_use_flattened(tmp_path):
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


def test_claude_code_tool_result_truncated():
    content = [{"type": "tool_result", "content": "x" * 1000}]
    result = _flatten_content(content)
    assert len(result) < 600  # "[Tool result]\n" + 500 chars


def test_claude_code_returns_none_for_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert _parse_jsonl(path) is None


def test_claude_code_skips_non_message_entries(tmp_path):
    entries = [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": "s1"},
        {"type": "file-history-snapshot", "messageId": "x", "sessionId": "s1"},
        {"type": "user", "sessionId": "s1", "uuid": "m1",
         "timestamp": "2024-01-01T10:00:00Z",
         "message": {"role": "user", "content": "real message"}},
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


def test_deepseek_parse_ts():
    assert _parse_ts(1.5) == 1500
    assert _parse_ts("2.25") == 2250
    assert _parse_ts(None) is None
    assert _parse_ts("nope") is None


def test_deepseek_metadata():
    result = _metadata({
        "model": "deepseek-r1",
        "thinking_enabled": True,
        "search_enabled": False,
        "accumulated_token_usage": 123,
    })
    assert result == {
        "model": "deepseek-r1",
        "thinking_enabled": True,
        "search_enabled": False,
        "tokens": 123,
    }


def test_deepseek_message_content_uses_real_payload_fields():
    result = _message_content({
        "content": "answer",
        "thinking_content": "reasoning",
        "search_results": [{"title": "Doc", "url": "https://example.com"}],
    })
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
            cursor = (params or {}).get("lte_cursor.seq_id")
            if cursor is None:
                return Resp({
                    "data": {
                        "biz_data": {
                            "chat_sessions": [
                                {"id": "a", "seq_id": 5, "updated_at": 5},
                                {"id": "b", "seq_id": 4, "updated_at": 4},
                            ],
                            "has_more": True,
                        }
                    }
                })
            if cursor == "3":
                return Resp({
                    "data": {
                        "biz_data": {
                            "chat_sessions": [
                                {"id": "b", "seq_id": 4, "updated_at": 4},
                                {"id": "c", "seq_id": 3, "updated_at": 3},
                            ],
                            "has_more": False,
                        }
                    }
                })
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
    con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, time_created INTEGER, time_updated INTEGER)")
    con.execute("INSERT INTO session(id, title, time_created, time_updated) VALUES ('s1', 'a', 1, 1000)")
    con.execute("INSERT INTO session(id, title, time_created, time_updated) VALUES ('s2', 'b', 2, 2000)")
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
                        IngestedPart(kind="search_result", text="raspberry hidden", searchable=False),
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
    assert db._fts_query("") == "\"\""
    assert db._fts_query("   ") == "\"\""


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

def test_windsurf_parse_timestamp_iso():
    from llm_archive.ingestors.windsurf import _parse_timestamp
    ts = _parse_timestamp("2024-03-27T04:26:48.000Z")
    assert ts is not None
    assert ts > 1711510000000  # ms since epoch


def test_windsurf_parse_timestamp_none():
    from llm_archive.ingestors.windsurf import _parse_timestamp
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None


def test_windsurf_convert_user_message():
    from llm_archive.ingestors.windsurf import _convert_to_thread
    conv = {
        "trajectoryId": "test-id",
        "turns": [
            {"role": "user", "text": "Hello Windsurf", "at": "2024-03-27T04:26:48.000Z"}
        ]
    }
    thread = _convert_to_thread(conv)
    assert thread.id == "windsurf:test-id"
    assert thread.source_id == "windsurf"
    assert len(thread.messages) == 1
    msg = thread.messages[0]
    assert msg.role == "user"
    assert msg.content == "Hello Windsurf"
    assert msg.parts[0].kind == "text"
    assert thread.title == "Hello Windsurf"


def test_windsurf_convert_assistant_with_thinking():
    from llm_archive.ingestors.windsurf import _convert_to_thread
    conv = {
        "trajectoryId": "test-id",
        "turns": [
            {
                "role": "assistant",
                "text": "The answer is 42",
                "thinking": "Let me calculate...",
                "at": "2024-03-27T04:27:00.000Z"
            }
        ]
    }
    thread = _convert_to_thread(conv)
    msg = thread.messages[0]
    assert msg.role == "assistant"
    assert "Let me calculate" in msg.content
    assert "The answer is 42" in msg.content
    assert len(msg.parts) == 2
    assert msg.parts[0].kind == "thinking"
    assert msg.parts[0].visible is False
    assert msg.parts[1].kind == "text"


def test_windsurf_convert_command():
    from llm_archive.ingestors.windsurf import _convert_to_thread
    conv = {
        "trajectoryId": "test-id",
        "turns": [
            {
                "role": "tool",
                "command": "ls",
                "args": ["-la"],
                "stdout": "total 10",
                "exitCode": 0,
                "at": "2024-03-27T04:27:30.000Z"
            }
        ]
    }
    thread = _convert_to_thread(conv)
    msg = thread.messages[0]
    assert msg.role == "tool"
    assert "ls -la" in msg.content
    assert "total 10" in msg.content
    assert msg.parts[0].kind == "tool_call"
    assert msg.parts[0].data["command"] == "ls -la"


def test_windsurf_convert_write_file():
    from llm_archive.ingestors.windsurf import _convert_to_thread
    conv = {
        "trajectoryId": "test-id",
        "turns": [
            {
                "role": "tool",
                "tool": "write_file",
                "path": "/tmp/test.txt",
                "content": "hello world",
                "at": "2024-03-27T04:28:00.000Z"
            }
        ]
    }
    thread = _convert_to_thread(conv)
    msg = thread.messages[0]
    assert msg.role == "tool"
    assert "/tmp/test.txt" in msg.content
    assert msg.parts[0].kind == "tool_call"
    assert msg.parts[0].data["path"] == "/tmp/test.txt"


def test_windsurf_convert_read_file():
    from llm_archive.ingestors.windsurf import _convert_to_thread
    conv = {
        "trajectoryId": "test-id",
        "turns": [
            {
                "role": "tool",
                "tool": "read_file",
                "path": "/etc/hosts",
                "at": "2024-03-27T04:28:30.000Z"
            }
        ]
    }
    thread = _convert_to_thread(conv)
    msg = thread.messages[0]
    assert "Read file" in msg.content
    assert msg.parts[0].data["path"] == "/etc/hosts"


def test_windsurf_convert_todo_list():
    from llm_archive.ingestors.windsurf import _convert_to_thread
    conv = {
        "trajectoryId": "test-id",
        "turns": [
            {
                "role": "tool",
                "tool": "todo_list",
                "todos": [{"content": "Implement feature"}, {"content": "Write tests"}],
                "at": "2024-03-27T04:29:00.000Z"
            }
        ]
    }
    thread = _convert_to_thread(conv)
    msg = thread.messages[0]
    assert msg.role == "tool"
    assert "TODO" in msg.content
    assert "Implement feature" in msg.content
    # Data contains raw todo objects with content field
    assert msg.parts[0].data["todos"] == [{"content": "Implement feature"}, {"content": "Write tests"}]


def test_windsurf_convert_checkpoint():
    from llm_archive.ingestors.windsurf import _convert_to_thread
    conv = {
        "trajectoryId": "test-id",
        "turns": [
            {
                "role": "checkpoint",
                "intent": "Fix the bug in auth module",
                "at": "2024-03-27T04:30:00.000Z"
            }
        ]
    }
    thread = _convert_to_thread(conv)
    msg = thread.messages[0]
    assert msg.role == "system"
    assert "Fix the bug" in msg.content
    assert msg.parts[0].kind == "system"
    assert msg.parts[0].visible is False


@pytest.mark.asyncio
async def test_windsurf_count_threads():
    # Windsurf count_threads returns 0 when CDP not available
    from llm_archive.ingestors.windsurf import WindsurfIngestor
    ingestor = WindsurfIngestor(cdp_port=59999)  # Use non-existent port
    count = await ingestor.count_threads()
    assert count == 0


def test_windsurf_full_conversation():
    from llm_archive.ingestors.windsurf import _convert_to_thread
    conv = {
        "trajectoryId": "conv-123",
        "turns": [
            {"role": "user", "text": "Create a Python script", "at": "2024-03-27T10:00:00Z"},
            {
                "role": "assistant",
                "text": "I'll create a script for you",
                "thinking": "User wants a Python script. I'll create a simple hello world.",
                "at": "2024-03-27T10:00:05Z"
            },
            {
                "role": "tool",
                "tool": "write_file",
                "path": "hello.py",
                "content": "print('hello')",
                "at": "2024-03-27T10:00:10Z"
            },
            {
                "role": "tool",
                "command": "python",
                "args": ["hello.py"],
                "stdout": "hello\n",
                "exitCode": 0,
                "at": "2024-03-27T10:00:15Z"
            },
        ]
    }
    thread = _convert_to_thread(conv)
    assert len(thread.messages) == 4
    assert thread.messages[0].role == "user"
    assert thread.messages[1].role == "assistant"
    assert thread.messages[2].role == "tool"
    assert thread.messages[3].role == "tool"
