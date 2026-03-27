"""Tests for database schema and parser output validation."""
from __future__ import annotations
import json
import textwrap
from pathlib import Path

import pytest

from llm_archive import db
from llm_archive.ingestors.claude_code import _parse_jsonl, _flatten_content
from llm_archive.ingestors.opencode import _build_thread
from llm_archive.schema import IngestedMessage, IngestedThread


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
    assert thread.source_id == "claude_code"
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
