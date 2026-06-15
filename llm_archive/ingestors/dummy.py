from __future__ import annotations

import os
import time
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedThread

SOURCE_ID = "dummy"
THREAD_ID = "dummy:e2e-canary"
SEARCH_TOKEN = "dummycanarytoken"


class DummyIngestor(BaseIngestor):
    """Deterministic, dependency-free ingestor backing the e2e test.

    Hidden: not listed in the user-facing INGESTORS catalog, so it never shows
    in `sources`, the enable/disable choices, or the default config. The
    service resolves it via the hidden registry in ingestors.__init__, so the
    e2e workflow can enable it through config and prove the scheduler actually
    runs a sync end to end.
    """

    source_id = SOURCE_ID

    def __init__(self) -> None:
        self._marker = os.environ.get("LLM_ARCHIVE_DUMMY_MARKER", "v1")

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        return None

    async def prepare(self) -> bool:
        return True

    async def count_threads(self, since: int | None = None) -> int:
        return 1

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        now = int(time.time() * 1000)
        yield IngestedThread(
            id=THREAD_ID,
            source_id=SOURCE_ID,
            title=f"e2e canary {self._marker}",
            created_at=now,
            updated_at=now,
            messages=[
                IngestedMessage(
                    id=f"{THREAD_ID}:user",
                    thread_id=THREAD_ID,
                    role="user",
                    content=f"service e2e probe {SEARCH_TOKEN} {self._marker}",
                    created_at=now,
                ),
                IngestedMessage(
                    id=f"{THREAD_ID}:assistant",
                    thread_id=THREAD_ID,
                    role="assistant",
                    content=f"ack {SEARCH_TOKEN} {self._marker}",
                    created_at=now,
                ),
            ],
        )
