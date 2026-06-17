from __future__ import annotations

from pathlib import Path

import pytest

from llm_archive.ingestors import get_ingestor
from llm_archive.ingestors.gemini import GeminiIngestor, _parse_json

FIXTURE = Path(__file__).parent / "fixture-gemini.json"


def test_parses_gemini_session_into_user_assistant_pairs():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    assert thread.source_id == "gemini"
    assert len(thread.messages) == 8
    assert [message.role for message in thread.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_extracts_user_text():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    users = [message.content for message in thread.messages if message.role == "user"]
    assert users == [
        "What files are in the current directory?",
        "Read the README.md file",
        "Thanks!",
        "Run a failing command",
    ]


def test_extracts_thoughts_as_reasoning_blocks_with_subject():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    reasoning = [part for part in thread.messages[1].parts if part.kind == "reasoning"]
    assert len(reasoning) == 2
    assert "Analyzing Request" in reasoning[0].text
    assert "directory contents" in reasoning[0].text
    assert "Choosing Tool" in reasoning[1].text


def test_maps_run_shell_command_to_bash():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    tool = next(part for part in thread.messages[1].parts if part.kind == "tool_call")
    assert tool.tool_call is not None
    assert tool.tool_call.name == "Bash"
    assert tool.tool_call.input == {"command": "ls -la"}


def test_maps_read_file_to_read():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    tool = next(part for part in thread.messages[3].parts if part.kind == "tool_call")
    assert tool.tool_call is not None
    assert tool.tool_call.name == "Read"
    assert tool.tool_call.input == {"file_path": "README.md"}


def test_extracts_tool_results_from_nested_function_response():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    tool = next(part for part in thread.messages[1].parts if part.kind == "tool_call")
    assert tool.tool_call is not None
    assert tool.tool_call.result is not None
    assert "README.md" in tool.tool_call.result
    assert "package.json" in tool.tool_call.result


def test_groups_empty_content_tool_call_with_follow_up_text():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    assistant = thread.messages[3]
    assert len([part for part in assistant.parts if part.kind == "tool_call"]) == 1
    text_parts = [part for part in assistant.parts if part.kind == "text"]
    assert len(text_parts) == 1
    assert "README.md contains" in text_parts[0].text


def test_handles_empty_thoughts_array():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    reasoning = [part for part in thread.messages[5].parts if part.kind == "reasoning"]
    assert len(reasoning) == 0


def test_marks_error_tool_calls():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    tool = next(part for part in thread.messages[7].parts if part.kind == "tool_call")
    assert tool.tool_call is not None
    assert tool.tool_call.is_error
    assert tool.tool_call.result == "cat: nonexistent.txt: No such file or directory"


def test_preserves_timestamps():
    thread = _parse_json(FIXTURE)

    assert thread is not None
    assert thread.created_at == 1_772_359_200_000
    assert thread.updated_at == 1_772_359_500_000
    assert thread.messages[0].created_at == 1_772_359_200_000


@pytest.mark.asyncio
async def test_gemini_ingestor_finds_chat_files(tmp_path):
    chats_dir = tmp_path / "project-hash" / "chats"
    chats_dir.mkdir(parents=True)
    chat = chats_dir / "session-test.json"
    chat.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    ingestor = GeminiIngestor(tmp_path)
    threads = [thread async for thread in ingestor.threads()]

    assert await ingestor.count_threads() == 1
    assert len(threads) == 1
    assert threads[0].source_id == "gemini"


def test_gemini_ingestor_registered():
    ingestor = get_ingestor("gemini")

    assert isinstance(ingestor, GeminiIngestor)
