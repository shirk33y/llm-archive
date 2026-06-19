from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

VALID_AUTH_MODES = {"cookies"}
WEB_INGESTORS = {"chatgpt", "claude", "deepseek"}
INGESTOR_ORDER = (
    "chatgpt", "claude", "deepseek", "claudecode", "codex",
    "cursor", "gemini", "opencode", "windsurf",
)
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)(ms|s|m|h|d)\s*$")


def config_path() -> Path:
    override = os.environ.get("LLM_ARCHIVE_CONFIG")
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return config_home / "llm-archive" / "config.toml"


class IngestorConfig(BaseModel):
    mode: str | None = None
    enabled: bool = False
    sync_interval_ms: int | None = None
    min_sync_interval_ms: int | None = None
    watch: bool | None = None
    browser: str | None = None
    profile: str | None = None
    browser_path: str | None = None
    browser_dir: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def _check_mode(self) -> IngestorConfig:
        if self.mode is not None and self.mode not in VALID_AUTH_MODES:
            valid = ", ".join(sorted(VALID_AUTH_MODES))
            raise ValueError(f"Invalid auth mode: {self.mode!r}. Expected: {valid}")
        return self


class EmbedConfig(BaseModel):
    auto: bool = True
    provider: str = "fastembed"
    model: str = "BAAI/bge-small-en-v1.5"


class SummarizeConfig(BaseModel):
    auto: bool = False
    model: str = "ollama/qwen2.5:7b"
    min_new_messages: int = Field(default=3, ge=0)


class AppConfig(BaseModel):
    browser_path: str | None = None
    browser_dir: str | None = None
    embed: EmbedConfig = Field(default_factory=EmbedConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    ingestors: dict[str, IngestorConfig] = Field(default_factory=dict)

    def ingestor(self, source_id: str) -> IngestorConfig:
        defaults = default_ingestor_config(source_id)
        override = self.ingestors.get(source_id)
        if not override:
            return IngestorConfig(**defaults.model_dump())
        merged = defaults.model_dump()
        merged.update({k: v for k, v in override.model_dump().items() if v is not None})
        merged["browser_path"] = merged.get("browser_path") or self.browser_path
        merged["browser_dir"] = merged.get("browser_dir") or self.browser_dir
        return IngestorConfig(**merged)


def _duration_str_to_ms(value: str) -> int:
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"Invalid duration: {value!r}")
    amount = float(match.group(1))
    unit = match.group(2)
    factor = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]
    return max(1, int(amount * factor))


def _ms_to_duration_str(ms: int) -> str:
    units = [("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000)]
    for suffix, factor in units:
        if ms >= factor:
            return f"{ms // factor}{suffix}"
    return f"{ms}ms"


def parse_duration_ms(value: str) -> int:
    return _duration_str_to_ms(value)


def format_duration_ms(ms: int | None) -> str:
    if ms is None:
        return "-"
    return _ms_to_duration_str(ms)


def default_ingestor_config(source_id: str) -> IngestorConfig:
    if source_id == "deepseek":
        return IngestorConfig(
            mode="cookies",
            sync_interval_ms=_duration_str_to_ms("30m"),
            min_sync_interval_ms=_duration_str_to_ms("10m"),
            watch=False,
        )
    if source_id in WEB_INGESTORS:
        return IngestorConfig(
            mode="cookies",
            sync_interval_ms=_duration_str_to_ms("30m"),
            min_sync_interval_ms=_duration_str_to_ms("30m"),
            watch=False,
        )
    return IngestorConfig(
        sync_interval_ms=_duration_str_to_ms("10s"),
        min_sync_interval_ms=_duration_str_to_ms("10s"),
        watch=True,
    )


def _raw_to_model(data: dict[str, Any]) -> AppConfig:
    ingestors: dict[str, dict[str, Any]] = {}
    for source_id, raw in data.get("ingestors", {}).items():
        entry: dict[str, Any] = {}
        for key in ("mode", "enabled", "watch", "browser", "profile", "browser_path", "browser_dir", "path"):
            if key in raw:
                entry[key] = raw[key]
        if "sync_interval" in raw:
            entry["sync_interval_ms"] = _duration_str_to_ms(raw["sync_interval"])
        if "min_sync_interval" in raw:
            entry["min_sync_interval_ms"] = _duration_str_to_ms(raw["min_sync_interval"])
        ingestors[source_id] = entry

    model_data: dict[str, Any] = {}
    if "browser_path" in data:
        model_data["browser_path"] = data["browser_path"]
    if "browser_dir" in data:
        model_data["browser_dir"] = data["browser_dir"]
    if "embed" in data:
        model_data["embed"] = data["embed"]
    if "summarize" in data:
        model_data["summarize"] = data["summarize"]
    if ingestors:
        model_data["ingestors"] = ingestors

    return AppConfig.model_validate(model_data)


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    ensure_config(path)
    data = tomllib.loads(path.read_text())
    return _raw_to_model(data)


def read_config_text(path: Path | None = None) -> str:
    path = path or config_path()
    ensure_config(path)
    return path.read_text()


def update_ingestor_config(source_id: str, values: dict[str, Any], path: Path | None = None) -> None:
    path = path or config_path()
    ensure_config(path)
    data = _raw_config(path)
    ingestors = data.setdefault("ingestors", {})
    raw = ingestors.setdefault(source_id, {})
    raw.update({key: value for key, value in values.items() if value is not None})
    _write_raw_config(data, path)


def _raw_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def ensure_config(path: Path | None = None) -> Path:
    path = path or config_path()
    if path.exists():
        return path
    data = _default_raw_config()
    browser_dir = _detect_browser_dir()
    if browser_dir:
        data["browser_dir"] = str(browser_dir)
    _write_raw_config(data, path)
    return path


def _default_raw_config() -> dict[str, Any]:
    return {
        "embed": {"auto": True, "provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"},
        "summarize": {"auto": False, "model": "ollama/qwen2.5:7b", "min_new_messages": 3},
        "ingestors": {
            source_id: _default_ingestor_table(source_id)
            for source_id in INGESTOR_ORDER
        },
    }


def _default_ingestor_table(source_id: str) -> dict[str, Any]:
    config = default_ingestor_config(source_id)
    row: dict[str, Any] = {"enabled": False}
    if config.mode:
        row["mode"] = config.mode
    if config.sync_interval_ms:
        row["sync_interval"] = _ms_to_duration_str(config.sync_interval_ms)
    if config.min_sync_interval_ms:
        row["min_sync_interval"] = _ms_to_duration_str(config.min_sync_interval_ms)
    if config.watch is not None:
        row["watch"] = config.watch
    return row


def _detect_browser_dir() -> Path | None:
    for path in _browser_roots():
        if path.exists() and any(path.glob("**/cookies.sqlite")):
            return path
    return None


def _browser_roots() -> list[Path]:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    home = Path.home()
    if os.sys.platform == "darwin":
        return [
            home / "Library" / "Application Support" / "Firefox" / "Profiles",
            home / "Library" / "Application Support" / "Waterfox" / "Profiles",
            home / "Library" / "Application Support" / "LibreWolf" / "Profiles",
        ]
    return [
        home / ".var" / "app" / "net.waterfox.waterfox" / ".waterfox",
        home / ".waterfox",
        home / "snap" / "waterfox" / "common" / ".waterfox",
        config_home / "mozilla" / "firefox",
        home / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / "config" / "mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
        home / ".snap" / "firefox" / "common" / ".mozilla" / "firefox",
    ]


def _write_raw_config(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_toml(data))


def _toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in data.items():
        if key in ("ingestors", "embed", "summarize") or isinstance(value, dict):
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    if lines:
        lines.append("")
    for section in ("embed", "summarize"):
        if section in data and isinstance(data[section], dict):
            lines.append(f"[{section}]")
            for key, value in data[section].items():
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("")
    ingestors = data.get("ingestors")
    if isinstance(ingestors, dict):
        for source_id in sorted(ingestors):
            lines.append(f"[ingestors.{source_id}]")
            for key, value in ingestors[source_id].items():
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return repr(str(value)).replace("'", '"')
