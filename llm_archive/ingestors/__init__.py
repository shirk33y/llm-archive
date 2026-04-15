from __future__ import annotations

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.ingestors.chatgpt import ChatGPTIngestor
from llm_archive.ingestors.claude import ClaudeIngestor
from llm_archive.ingestors.claudecode import ClaudeCodeIngestor
from llm_archive.ingestors.deepseek import DeepseekIngestor
from llm_archive.ingestors.opencode import OpenCodeIngestor
from llm_archive.ingestors.windsurf import WindsurfIngestor

INGESTORS: dict[str, type[BaseIngestor]] = {
    "claudecode": ClaudeCodeIngestor,
    "opencode": OpenCodeIngestor,
    "windsurf": WindsurfIngestor,
    "claude": ClaudeIngestor,
    "deepseek": DeepseekIngestor,
    "chatgpt": ChatGPTIngestor,
}


def get_ingestor(source_id: str) -> BaseIngestor:
    cls = INGESTORS.get(source_id)
    if not cls:
        available = ", ".join(INGESTORS)
        raise ValueError(f"Unknown source '{source_id}'. Available: {available}")
    return cls()
