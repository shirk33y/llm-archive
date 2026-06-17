from __future__ import annotations

from pathlib import Path

import pytest

from llm_archive.ingestors import get_ingestor
from llm_archive.ingestors.cursor import CursorIngestor, _parse_jsonl

FIXTURE = Path(__file__).parent / "fixture-cursor.jsonl"


def test_parses_cursor_entries_into_messages():
    thread = _parse_jsonl(FIXTURE)

    assert thread is not None
    assert thread.source_id == "cursor"
    assert [message.role for message in thread.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_strips_user_query_tags():
    thread = _parse_jsonl(FIXTURE)

    assert thread is not None
    assert thread.messages[0].content == "scan for ble devices"
    assert thread.messages[2].content == "connect to the first one"


def test_groups_consecutive_assistant_messages():
    thread = _parse_jsonl(FIXTURE)

    assert thread is not None
    first_assistant = thread.messages[1]
    assert len(first_assistant.parts) == 2
    assert "Planning scan" in first_assistant.parts[0].text
    assert "Found 3 devices" in first_assistant.parts[1].text


def test_reclassifies_all_but_last_assistant_block_as_reasoning():
    thread = _parse_jsonl(FIXTURE)

    assert thread is not None
    first_assistant = thread.messages[1]
    assert [part.kind for part in first_assistant.parts] == ["reasoning", "text"]
    assert [part.kind for part in thread.messages[3].parts] == ["text"]


def test_has_no_timestamps_when_fixture_has_none():
    thread = _parse_jsonl(FIXTURE)

    assert thread is not None
    assert thread.messages[0].created_at is None
    assert thread.created_at is None


@pytest.mark.asyncio
async def test_cursor_ingestor_finds_agent_transcripts(tmp_path):
    session_dir = tmp_path / "project" / "agent-transcripts" / "session-1"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "transcript.jsonl"
    transcript.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    ingestor = CursorIngestor(tmp_path)
    threads = [thread async for thread in ingestor.threads()]

    assert await ingestor.count_threads() == 1
    assert len(threads) == 1
    assert threads[0].source_id == "cursor"


def test_cursor_ingestor_registered():
    ingestor = get_ingestor("cursor")

    assert isinstance(ingestor, CursorIngestor)
