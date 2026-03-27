from __future__ import annotations
from abc import ABC, abstractmethod
from typing import AsyncIterator

from llm_archive.schema import IngestedThread


class BaseIngestor(ABC):
    source_id: str  # e.g. 'claude_code', 'claude', 'opencode', 'windsurf'

    @abstractmethod
    async def requires_auth(self) -> bool:
        """Return True if this source needs Playwright login."""
        ...

    @abstractmethod
    async def init(self, **kwargs) -> None:
        """First-time setup: auth, config, path discovery."""
        ...

    @abstractmethod
    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        """Yield threads, optionally only those updated after `since` (unix ms)."""
        ...
