from __future__ import annotations
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedThread

# TODO: verify path — Windsurf cascade files (.pb) appear to be encrypted/binary.
# Known location: ~/.codeium/windsurf/cascade/*.pb
# These cannot be parsed without the encryption key.
# If Windsurf exposes a plain-text export format in the future, implement here.
# Use --path override via `llm-archive init windsurf --path <dir>` when available.

CANDIDATE_PATHS = [
    Path.home() / ".codeium" / "windsurf" / "cascade",
    Path.home() / ".windsurf" / "cascade",
]


class WindsurfIngestor(BaseIngestor):
    source_id = "windsurf"

    def __init__(self, path: Path | None = None):
        self.path = path

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        override = kwargs.get("path")
        if override:
            self.path = Path(override)

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        candidate = self.path
        if candidate is None:
            for p in CANDIDATE_PATHS:
                if p.exists():
                    candidate = p
                    break

        if candidate is None or not candidate.exists():
            raise RuntimeError(
                "Windsurf conversation path not found. "
                "Use `llm-archive init windsurf --path <dir>` to specify the location manually.\n"
                "Known issue: Windsurf stores cascade files as encrypted .pb blobs — "
                "parsing may not be possible without the encryption key."
            )

        pb_files = list(candidate.glob("*.pb"))
        if not pb_files:
            raise RuntimeError(f"No .pb files found in {candidate}")

        raise NotImplementedError(
            f"Found {len(pb_files)} Windsurf .pb files in {candidate}, "
            "but they appear to be encrypted binary protobuf. "
            "Cannot parse without encryption key. "
            "TODO: investigate if Windsurf exposes a plain export format."
        )
        # make type checker happy
        return
        yield  # type: ignore[misc]
