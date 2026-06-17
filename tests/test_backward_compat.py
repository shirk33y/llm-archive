from __future__ import annotations

from llm_archive.schema import IngestedMessage, IngestedPart, ToolCall
from llm_archive.db import _message_parts
from llm_archive.text import _parse_parts, _parse_part
from llm_archive.cli import _part_label, _part_data


def test_parse_parts_legacy_tool_call():
    text = "[Tool: Bash]\nls -la"
    parts = _parse_parts(text)
    assert len(parts) == 1
    assert parts[0].kind == "tool_call"
    assert parts[0].data == {"tag": "Tool: Bash", "name": "Bash"}
    assert parts[0].text == "ls -la"


def test_parse_parts_legacy_tool_result():
    text = "[Tool result]\nfile contents here"
    parts = _parse_parts(text)
    assert len(parts) == 1
    assert parts[0].kind == "tool_result"
    assert parts[0].text == "file contents here"


def test_parse_parts_legacy_thinking():
    text = "[Reasoning]\nlet me think about this"
    parts = _parse_parts(text)
    assert len(parts) == 1
    assert parts[0].kind == "reasoning"


def test_parse_parts_legacy_mixed_content():
    text = """Hello

[Tool: Bash]
echo hi

[Tool result]
hi"""
    parts = _parse_parts(text)
    assert len(parts) == 3
    assert parts[0].kind == "text"
    assert parts[1].kind == "tool_call"
    assert parts[2].kind == "tool_result"


def test_parse_part_tool_call_label_renders():
    part = _parse_part("[Tool: Bash]\nls -la\nresult")
    data = part.data
    label = _part_label("tool_call", data)
    assert label == "[Tool: Bash]"


def test_parse_part_tool_call_label_no_data():
    label = _part_label("tool_call", {})
    assert label == "[tool_call]"


def test_parse_part_tool_call_label_with_part():
    part = {"tool_name": "Bash", "kind": "tool_call"}
    label = _part_label("tool_call", {}, part)
    assert label == "[Tool: Bash]"


def test_parse_part_tool_call_label_no_fallback():
    part = {"tool_name": None, "kind": "tool_call"}
    label = _part_label("tool_call", {}, part)
    assert label == "[tool_call]"


def test_message_parts_fallback_content():
    msg = IngestedMessage(
        id="test:1",
        thread_id="test:t1",
        role="assistant",
        content="[Tool: Bash]\nls -la\n\n[Tool result]\nlots of files",
        created_at=1000,
        parts=[],
    )
    parts = _message_parts(msg)
    assert len(parts) == 2
    assert parts[0].kind == "tool_call"
    assert parts[0].data["name"] == "Bash"
    assert parts[1].kind == "tool_result"


def test_message_parts_uses_structured_parts_first():
    tc = ToolCall(tool_use_id="tu1", name="Bash")
    part = IngestedPart(kind="tool_call", tool_call=tc)
    msg = IngestedMessage(
        id="test:2",
        thread_id="test:t1",
        role="assistant",
        content="different from parts",
        created_at=1000,
        parts=[part],
    )
    parts = _message_parts(msg)
    assert len(parts) == 1
    assert parts[0].tool_call.name == "Bash"
    assert parts[0].tool_call.tool_use_id == "tu1"


def test_message_parts_empty_content():
    msg = IngestedMessage(
        id="test:3",
        thread_id="test:t1",
        role="assistant",
        content="",
        created_at=1000,
    )
    parts = _message_parts(msg)
    assert parts == []


def test_part_label_tool_call_via_part_dict():
    cases = [
        ({"kind": "tool_call", "tool_name": "WebSearch"}, {}, "[Tool: WebSearch]"),
        ({"kind": "tool_call", "tool_name": "Bash"}, {}, "[Tool: Bash]"),
        ({"kind": "tool_call", "tool_name": None}, {}, "[tool_call]"),
        ({"kind": "tool_call"}, {"name": "Read"}, "[Tool: Read]"),
    ]
    for part, data, expected in cases:
        label = _part_label("tool_call", data, part)
        assert label == expected, f"Expected {expected}, got {label} for part={part} data={data}"


def test_part_label_reasoning():
    label = _part_label("reasoning", {})
    assert label == "[Reasoning]"


def test_part_data_handles_none():
    data = _part_data(None)
    assert data == {}


def test_part_data_handles_json():
    data = _part_data('{"name": "Bash"}')
    assert data == {"name": "Bash"}


def test_part_data_handles_invalid_json():
    data = _part_data("not json")
    assert data == {}
