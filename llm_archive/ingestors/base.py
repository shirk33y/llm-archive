from __future__ import annotations
from abc import ABC, abstractmethod
from typing import AsyncIterator

from llm_archive.schema import IngestedThread


class BaseIngestor(ABC):
    source_id: str  # e.g. 'claudecode', 'claude', 'opencode', 'windsurf'

    @abstractmethod
    async def requires_auth(self) -> bool:
        """Return True if this source needs authentication."""
        ...

    @abstractmethod
    async def init(self, **kwargs) -> None:
        """First-time setup: auth, config, path discovery."""
        ...

    @abstractmethod
    def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        """Yield threads, optionally only those updated after `since` (unix ms)."""
        ...

    async def count_threads(self, since: int | None = None) -> int | None:
        return None

    async def prepare(self) -> bool:
        """Pre-flight check before progress bar starts. 
        
        Returns True if ready to sync, False to skip this source.
        Can be used for interactive prompts that should happen before progress display.
        """
        return True
