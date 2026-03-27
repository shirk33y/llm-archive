from __future__ import annotations
from llm_archive.ingestors.base import BaseIngestor
from llm_archive.ingestors.claude_code import ClaudeCodeIngestor
from llm_archive.ingestors.opencode import OpenCodeIngestor
from llm_archive.ingestors.windsurf import WindsurfIngestor
from llm_archive.ingestors.claude import ClaudeIngestor

# Registry: source_id -> ingestor class
# To add a new source: create ingestors/<name>.py, implement BaseIngestor, add here.
INGESTORS: dict[str, type[BaseIngestor]] = {
    "claude_code": ClaudeCodeIngestor,
    "opencode": OpenCodeIngestor,
    "windsurf": WindsurfIngestor,
    "claude": ClaudeIngestor,
}


def get_ingestor(source_id: str) -> BaseIngestor:
    cls = INGESTORS.get(source_id)
    if not cls:
        available = ", ".join(INGESTORS)
        raise ValueError(f"Unknown source '{source_id}'. Available: {available}")
    return cls()
