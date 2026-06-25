from __future__ import annotations

import os
from collections.abc import Mapping

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.ingestors.chatgpt import ChatGPTIngestor
from llm_archive.ingestors.claude import ClaudeIngestor
from llm_archive.ingestors.claudecode import ClaudeCodeIngestor
from llm_archive.ingestors.deepseek import DeepseekIngestor
from llm_archive.ingestors.codex import CodexIngestor
from llm_archive.ingestors.cursor import CursorIngestor
from llm_archive.ingestors.dummy import DummyIngestor
from llm_archive.ingestors.gemini import GeminiIngestor
from llm_archive.ingestors.opencode import OpenCodeIngestor
from llm_archive.ingestors.windsurf import WindsurfIngestor

INGESTORS: dict[str, type[BaseIngestor]] = {
    "claudecode": ClaudeCodeIngestor,
    "codex": CodexIngestor,
    "cursor": CursorIngestor,
    "gemini": GeminiIngestor,
    "opencode": OpenCodeIngestor,
    "windsurf": WindsurfIngestor,
    "claude": ClaudeIngestor,
    "deepseek": DeepseekIngestor,
    "chatgpt": ChatGPTIngestor,
}

TEST_SOURCE_ENV = "LLM_ARCHIVE_ENABLE_TEST_SOURCES"

TEST_INGESTORS: dict[str, type[BaseIngestor]] = {
    "dummy": DummyIngestor,
}


def test_sources_enabled() -> bool:
    return os.environ.get(TEST_SOURCE_ENV) == "1"


def service_source_ids(source_ids: Mapping[str, object]) -> list[str]:
    return [
        source_id
        for source_id in source_ids
        if source_id not in TEST_INGESTORS or test_sources_enabled()
    ]


def get_ingestor(source_id: str) -> BaseIngestor:
    cls = INGESTORS.get(source_id)
    if cls is None and test_sources_enabled():
        cls = TEST_INGESTORS.get(source_id)
    if not cls:
        available = ", ".join(INGESTORS)
        raise ValueError(f"Unknown source '{source_id}'. Available: {available}")
    return cls()
