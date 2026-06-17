from __future__ import annotations
import re

from llm_archive.schema import IngestedPart
from llm_archive.unicode import sanitize_text


_INJECTION_TAGS = re.compile(
    r"<(?:"
    r"ide_opened_file"
    r"|local-command-caveat"
    r"|command-name"
    r"|command-message"
    r"|command-args"
    r"|system-reminder"
    r"|user-prompt-submit-hook"
    r")[\s\S]*?</[^>]+>",
    re.DOTALL,
)


def _strip_content(text: str) -> str:
    if not text:
        return text
    return _INJECTION_TAGS.sub("", sanitize_text(text)).strip()


def clean_content(text: str) -> str:
    if not text:
        return text
    cleaned = _INJECTION_TAGS.sub("", sanitize_text(text))
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_parts(text: str) -> list[IngestedPart]:
    body = _strip_content(text)
    if not body:
        return []
    parts = [_parse_part(block) for block in re.split(r"\n\s*\n+", body) if block.strip()]
    return [part for part in parts if part.text or part.data]


def _parse_part(text: str) -> IngestedPart:
    match = re.match(r"^\s*\[([^\]\n]{1,120})\]\s*(.*)$", text, re.DOTALL)
    if not match:
        return IngestedPart(kind="text", text=text)
    tag = match.group(1)
    body = match.group(2).strip()
    kind = _part_kind(tag)
    visible, searchable = _part_flags(kind)
    data = {"tag": tag}
    if tag.startswith("Tool: "):
        data["name"] = tag.removeprefix("Tool: ")
    return IngestedPart(kind=kind, text=body, data=data, visible=visible, searchable=searchable)


def _part_kind(tag: str) -> str:
    if tag.startswith("Tool: "):
        return "tool_call"
    if tag == "Tool result":
        return "tool_result"
    if tag in {"Thinking", "Reasoning"}:
        return "reasoning"
    if tag == "Search":
        return "search_query"
    if tag == "Search results":
        return "search_result"
    if tag.startswith("citation:"):
        return "citation"
    if tag.startswith("Request interrupted"):
        return "status"
    if tag.endswith("-mode") or tag.startswith("SYSTEM DIRECTIVE:"):
        return "directive"
    return "unknown"


def _part_flags(kind: str) -> tuple[bool, bool]:
    if kind in {"citation", "status"}:
        return True, False
    if kind == "directive":
        return True, False
    return True, True
