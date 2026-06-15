from __future__ import annotations

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.ingestors.chatgpt import ChatGPTIngestor
from llm_archive.ingestors.claude import ClaudeIngestor
from llm_archive.ingestors.claudecode import ClaudeCodeIngestor
from llm_archive.ingestors.deepseek import DeepseekIngestor
from llm_archive.ingestors.codex import CodexIngestor
from llm_archive.ingestors.dummy import DummyIngestor
from llm_archive.ingestors.opencode import OpenCodeIngestor
from llm_archive.ingestors.windsurf import WindsurfIngestor

INGESTORS: dict[str, type[BaseIngestor]] = {
    "claudecode": ClaudeCodeIngestor,
    "codex": CodexIngestor,
    "opencode": OpenCodeIngestor,
    "windsurf": WindsurfIngestor,
    "claude": ClaudeIngestor,
    "deepseek": DeepseekIngestor,
    "chatgpt": ChatGPTIngestor,
}

# Hidden sources: resolvable by the service/jobs but never advertised in the
# UI catalog (`sources`, enable/disable choices) or the default config. Used to
# exercise the scheduler with deterministic data without real provider auth.
HIDDEN_INGESTORS: dict[str, type[BaseIngestor]] = {
    "dummy": DummyIngestor,
}


def get_ingestor(source_id: str) -> BaseIngestor:
    cls = INGESTORS.get(source_id) or HIDDEN_INGESTORS.get(source_id)
    if not cls:
        available = ", ".join(INGESTORS)
        raise ValueError(f"Unknown source '{source_id}'. Available: {available}")
    return cls()
