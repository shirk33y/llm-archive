from __future__ import annotations

from llm_archive.text import (
    _part_flags,
    _part_kind,
    _parse_part,
    _parse_parts,
    _strip_content,
    clean_content,
)


def test_clean_content_empty():
    assert clean_content("") == ""
    assert clean_content(None) is None


def test_clean_content_strips_injection_tags():
    result = clean_content("hello <ide_opened_file>foo.py</ide_opened_file> world")
    assert result == "hello world"


def test_clean_content_normalizes_whitespace():
    result = clean_content("hello    world\n\n  foo")
    assert result == "hello world foo"


def test_clean_content_passthrough():
    result = clean_content("hello world")
    assert result == "hello world"


def test_strip_content_empty():
    assert _strip_content("") == ""
    assert _strip_content(None) is None


def test_strip_content_removes_injection_tags():
    result = _strip_content("before <system-reminder>something</system-reminder> after")
    assert result == "before  after"


def test_parse_parts_empty_body():
    assert _parse_parts("") == []
    assert _parse_parts("  ") == []


def test_parse_parts_single_text_block():
    result = _parse_parts("hello world")
    assert len(result) == 1
    assert result[0].kind == "text"
    assert result[0].text == "hello world"


def test_parse_parts_multiple_blocks():
    result = _parse_parts("first block\n\nsecond block")
    assert len(result) == 2
    assert result[0].kind == "text"
    assert result[0].text == "first block"
    assert result[1].text == "second block"


def test_parse_parts_tagged_blocks():
    result = _parse_parts("[Thinking]\nplanning\n\n[Tool: Bash]\nls -la")
    assert len(result) == 2
    assert result[0].kind == "reasoning"
    assert result[0].text == "planning"
    assert result[1].kind == "tool_call"
    assert result[1].text == "ls -la"
    assert result[1].data["name"] == "Bash"


def test_parse_parts_keeps_tagged_blocks_with_empty_text():
    result = _parse_parts("[Tool result]\n\n\n[Search]\nfoo")
    assert len(result) == 2
    assert result[0].kind == "tool_result"
    assert result[0].text == ""
    assert result[1].kind == "search_query"
    assert result[1].text == "foo"


def test_parse_parts_filters_untagged_empty_blocks():
    result = _parse_parts("hello\n\n  \n\nworld")
    assert len(result) == 2


def test_parse_part_untagged():
    part = _parse_part("just some text")
    assert part.kind == "text"
    assert part.text == "just some text"


def test_parse_part_tagged_reasoning():
    part = _parse_part("[Reasoning]\nthinking text")
    assert part.kind == "reasoning"
    assert part.text == "thinking text"


def test_parse_part_tool_call():
    part = _parse_part("[Tool: Bash]\nls")
    assert part.kind == "tool_call"
    assert part.text == "ls"
    assert part.data["name"] == "Bash"
    assert part.data["tag"] == "Tool: Bash"


def test_parse_part_tool_result():
    part = _parse_part("[Tool result]\noutput text")
    assert part.kind == "tool_result"
    assert part.text == "output text"


def test_parse_part_search():
    part = _parse_part("[Search]\nquery text")
    assert part.kind == "search_query"


def test_parse_part_search_results():
    part = _parse_part("[Search results]\nresults")
    assert part.kind == "search_result"


def test_parse_part_citation():
    part = _parse_part("[citation:3]\nsource text")
    assert part.kind == "citation"


def test_parse_part_request_interrupted():
    part = _parse_part("[Request interrupted by user]\nstopped")
    assert part.kind == "status"


def test_parse_part_directive():
    part = _parse_part("[search-mode]\ndeep")
    assert part.kind == "directive"
    part2 = _parse_part("[SYSTEM DIRECTIVE: x]\nvalue")
    assert part2.kind == "directive"


def test_parse_part_unknown():
    part = _parse_part("[SomeRandomTag]\nvalue")
    assert part.kind == "unknown"


def test_part_kind_all_variants():
    assert _part_kind("Tool: Bash") == "tool_call"
    assert _part_kind("Tool result") == "tool_result"
    assert _part_kind("Thinking") == "reasoning"
    assert _part_kind("Reasoning") == "reasoning"
    assert _part_kind("Search") == "search_query"
    assert _part_kind("Search results") == "search_result"
    assert _part_kind("citation:5") == "citation"
    assert _part_kind("Request interrupted by user") == "status"
    assert _part_kind("search-mode") == "directive"
    assert _part_kind("SYSTEM DIRECTIVE: something") == "directive"
    assert _part_kind("other") == "unknown"


def test_part_flags():
    assert _part_flags("citation") == (True, False)
    assert _part_flags("status") == (True, False)
    assert _part_flags("directive") == (True, False)
    assert _part_flags("text") == (True, True)
    assert _part_flags("tool_call") == (True, True)
    assert _part_flags("tool_result") == (True, True)
