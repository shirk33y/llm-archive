from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_AUTH_MODES = {"cookies", "cdp"}


def config_path() -> Path:
    override = os.environ.get("LLM_ARCHIVE_CONFIG")
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return config_home / "llm-archive" / "config.toml"


@dataclass(frozen=True)
class IngestorConfig:
    mode: str | None = None


@dataclass(frozen=True)
class AppConfig:
    browser_path: str | None = None
    browser_dir: str | None = None
    ingestors: dict[str, IngestorConfig] | None = None

    def ingestor(self, source_id: str) -> IngestorConfig:
        return (self.ingestors or {}).get(source_id, IngestorConfig())


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.exists():
        return AppConfig(ingestors={})

    data = tomllib.loads(path.read_text())
    ingestors: dict[str, IngestorConfig] = {}

    for source_id, raw in _table(data.get("ingestors")).items():
        mode = _optional_str(raw.get("mode"), f"ingestors.{source_id}.mode")
        if mode is not None and mode not in VALID_AUTH_MODES:
            valid = ", ".join(sorted(VALID_AUTH_MODES))
            raise ValueError(f"Invalid auth mode for {source_id}: {mode!r}. Expected: {valid}")
        ingestors[source_id] = IngestorConfig(mode=mode)

    return AppConfig(
        browser_path=_optional_str(data.get("browser_path"), "browser_path"),
        browser_dir=_optional_str(data.get("browser_dir"), "browser_dir"),
        ingestors=ingestors,
    )


def _table(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("ingestors must be a TOML table")
    for key, child in value.items():
        if not isinstance(child, dict):
            raise ValueError(f"ingestors.{key} must be a TOML table")
    return value


def _optional_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value
