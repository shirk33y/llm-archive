"""Ported tests from claude-replay test-parser.mjs for Claude Code ingestor."""

from __future__ import annotations
import json
from pathlib import Path

from llm_archive.ingestors.claudecode import _parse_jsonl, _process_content_blocks

FIXTURE = Path(__file__).parent / "fixture-claude-code.jsonl"


def _make_jsonl(entries: list[dict]) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)


def test_parses_all_messages():
    thread = _parse_jsonl(FIXTURE)
    assert thread is not None
    assert len(thread.messages) == 8


def test_extracts_user_text():
    thread = _parse_jsonl(FIXTURE)
    assert thread.messages[0].role == "user"
    assert thread.messages[0].content == "Hello, what is 2+2?" 


def test_extracts_thinking_blocks():
    thread = _parse_jsonl(FIXTURE)
    parts = thread.messages[1].parts
    thinking = [p for p in parts if p.kind == "reasoning"]
    assert len(thinking) == 1
    assert "simple math" in thinking[0].text


def test_extracts_text_blocks():
    thread = _parse_jsonl(FIXTURE)
    parts = thread.messages[1].parts
    text = [p for p in parts if p.kind == "text"]
    assert len(text) == 1
    assert "2 + 2 = 4" in text[0].text


def test_extracts_tool_call_with_name_and_input():
    thread = _parse_jsonl(FIXTURE)
    tool_call_parts = [p for p in thread.messages[3].parts if p.kind == "tool_call"]
    assert len(tool_call_parts) == 1
    tc = tool_call_parts[0].tool_call
    assert tc is not None
    assert tc.name == "Read"
    assert tc.input == {"file_path": "/tmp/test.txt"}
    assert tc.tool_use_id == "tool_1"


def test_extracts_tool_result_with_linked_tool_use_id():
    thread = _parse_jsonl(FIXTURE)
    result_parts = [p for p in thread.messages[4].parts if p.kind == "tool_result"]
    assert len(result_parts) == 1
    tc = result_parts[0].tool_call
    assert tc is not None
    assert tc.tool_use_id == "tool_1"
    assert tc.result == "file contents here"
    assert not tc.is_error


def test_tool_call_not_truncated():
    thread = _parse_jsonl(FIXTURE)
    result_parts = [p for p in thread.messages[4].parts if p.kind == "tool_result"]
    assert result_parts[0].text == "file contents here"


def test_flat_content_backward_compatible():
    thread = _parse_jsonl(FIXTURE)
    msg = thread.messages[3]
    assert "[Tool: Read]" in msg.content

    msg4 = thread.messages[4]
    assert "[Tool result]" in msg4.content
    assert "file contents here" in msg4.content


def test_thinking_not_searchable():
    thread = _parse_jsonl(FIXTURE)
    parts = thread.messages[1].parts
    thinking = [p for p in parts if p.kind == "reasoning"]
    assert not thinking[0].searchable


def test_skips_non_message_entries():
    entries = [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": "s1"},
        {"type": "session-id", "id": "s1"},
        {"type": "file-history-snapshot", "messageId": "x", "sessionId": "s1"},
        {"type": "user", "sessionId": "s1", "uuid": "m1",
         "timestamp": "2024-01-01T10:00:00Z",
         "message": {"role": "user", "content": "real message"}},
    ]
    text = _make_jsonl(entries)
    path = FIXTURE.parent / "_tmp_skip_non_msg.jsonl"
    path.write_text(text)
    try:
        thread = _parse_jsonl(path)
        assert thread is not None
        assert len(thread.messages) == 1
        assert thread.messages[0].content == "real message"
    finally:
        path.unlink(missing_ok=True)


def test_empty_content_skipped():
    entries = [
        {"type": "user", "sessionId": "s1", "uuid": "m1",
         "timestamp": "2024-01-01T10:00:00Z",
         "message": {"role": "user", "content": ""}},
        {"type": "assistant", "sessionId": "s1", "uuid": "m2",
         "timestamp": "2024-01-01T10:00:05Z",
         "message": {"role": "assistant", "content": "response"}},
    ]
    text = _make_jsonl(entries)
    path = FIXTURE.parent / "_tmp_empty.jsonl"
    path.write_text(text)
    try:
        thread = _parse_jsonl(path)
        assert thread is not None
        assert len(thread.messages) == 1
        assert thread.messages[0].role == "assistant"
    finally:
        path.unlink(missing_ok=True)


def test_pending_tool_use_tracked_across_entries():
    """Tool_use without tool_result still produces a tool_call part."""
    thread = _parse_jsonl(FIXTURE)
    tool_call = [p for p in thread.messages[3].parts if p.kind == "tool_call"]
    assert len(tool_call) == 1
    assert tool_call[0].tool_call.name == "Read"


def test_tool_use_id_matches_across_messages():
    thread = _parse_jsonl(FIXTURE)
    tool_call = None
    tool_result = None
    for msg in thread.messages:
        for p in msg.parts:
            if p.kind == "tool_call" and p.tool_call and p.tool_call.tool_use_id == "tool_1":
                tool_call = p.tool_call
            if p.kind == "tool_result" and p.tool_call and p.tool_call.tool_use_id == "tool_1":
                tool_result = p.tool_call
    assert tool_call is not None
    assert tool_result is not None
    assert tool_call.name == "Read"
    assert tool_result.result == "file contents here"


def test_process_content_blocks_tool_use():
    pending = {}
    content, parts = _process_content_blocks([
        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "echo hi"}},
    ], pending)
    assert parts[0].kind == "tool_call"
    assert parts[0].tool_call.name == "Bash"
    assert parts[0].tool_call.input == {"command": "echo hi"}


def test_process_content_blocks_tool_result_linking():
    pending = {"tu1": {"name": "Bash", "input": {"command": "echo hi"}}}
    content, parts = _process_content_blocks([
        {"type": "tool_result", "tool_use_id": "tu1", "content": "hi", "is_error": False},
    ], pending)
    assert parts[0].kind == "tool_result"
    assert parts[0].tool_call.result == "hi"
    assert parts[0].tool_call.name == "Bash"


def test_process_content_blocks_tool_result_error():
    pending = {"tu2": {"name": "Bash", "input": {"command": "false"}}}
    content, parts = _process_content_blocks([
        {"type": "tool_result", "tool_use_id": "tu2", "content": "exit 1", "is_error": True},
    ], pending)
    assert parts[0].tool_call.is_error
    assert parts[0].tool_call.result == "exit 1"


def test_process_content_blocks_returns_none_for_empty():
    path = FIXTURE.parent / "_tmp_empty_file.jsonl"
    path.write_text("")
    try:
        assert _parse_jsonl(path) is None
    finally:
        path.unlink(missing_ok=True)


def test_returns_none_for_empty_file():
    path = FIXTURE.parent / "_tmp_empty_ef.jsonl"
    path.write_text("")
    try:
        assert _parse_jsonl(path) is None
    finally:
        path.unlink(missing_ok=True)
