from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_AUTH_MODES = {"cookies", "cdp"}
WEB_INGESTORS = {"chatgpt", "claude", "deepseek"}
FILE_INGESTORS = {"claudecode", "codex", "cursor", "gemini", "opencode", "windsurf"}
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


@dataclass(frozen=True)
class IngestorConfig:
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


@dataclass(frozen=True)
class AppConfig:
    browser_path: str | None = None
    browser_dir: str | None = None
    ingestors: dict[str, IngestorConfig] | None = None

    def ingestor(self, source_id: str) -> IngestorConfig:
        defaults = default_ingestor_config(source_id)
        override = (self.ingestors or {}).get(source_id)
        if not override:
            return defaults
        return IngestorConfig(
            mode=override.mode if override.mode is not None else defaults.mode,
            enabled=override.enabled,
            sync_interval_ms=override.sync_interval_ms or defaults.sync_interval_ms,
            min_sync_interval_ms=override.min_sync_interval_ms or defaults.min_sync_interval_ms,
            watch=override.watch if override.watch is not None else defaults.watch,
            browser=override.browser,
            profile=override.profile,
            browser_path=override.browser_path or self.browser_path,
            browser_dir=override.browser_dir or self.browser_dir,
            path=override.path,
        )


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    ensure_config(path)

    data = tomllib.loads(path.read_text())
    ingestors: dict[str, IngestorConfig] = {}

    for source_id, raw in _table(data.get("ingestors")).items():
        mode = _optional_str(raw.get("mode"), f"ingestors.{source_id}.mode")
        if mode is not None and mode not in VALID_AUTH_MODES:
            valid = ", ".join(sorted(VALID_AUTH_MODES))
            raise ValueError(f"Invalid auth mode for {source_id}: {mode!r}. Expected: {valid}")
        enabled = _optional_bool(raw.get("enabled"), f"ingestors.{source_id}.enabled")
        ingestors[source_id] = IngestorConfig(
            mode=mode,
            enabled=True if enabled is None else enabled,
            sync_interval_ms=_optional_duration(
                raw.get("sync_interval"), f"ingestors.{source_id}.sync_interval"
            ),
            min_sync_interval_ms=_optional_duration(
                raw.get("min_sync_interval"), f"ingestors.{source_id}.min_sync_interval"
            ),
            watch=_optional_bool(raw.get("watch"), f"ingestors.{source_id}.watch"),
            browser=_optional_str(raw.get("browser"), f"ingestors.{source_id}.browser"),
            profile=_optional_str(raw.get("profile"), f"ingestors.{source_id}.profile"),
            browser_path=_optional_str(
                raw.get("browser_path"), f"ingestors.{source_id}.browser_path"
            ),
            browser_dir=_optional_str(raw.get("browser_dir"), f"ingestors.{source_id}.browser_dir"),
            path=_optional_str(raw.get("path"), f"ingestors.{source_id}.path"),
        )

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


def _optional_bool(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _optional_duration(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a duration string")
    return parse_duration_ms(value)


def parse_duration_ms(value: str) -> int:
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"Invalid duration: {value!r}")
    amount = float(match.group(1))
    unit = match.group(2)
    factor = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]
    return max(1, int(amount * factor))


def format_duration_ms(ms: int | None) -> str:
    if ms is None:
        return "-"
    units = [("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000)]
    for suffix, factor in units:
        if ms >= factor and ms % factor == 0:
            return f"{ms // factor}{suffix}"
    return f"{ms}ms"


def default_ingestor_config(source_id: str) -> IngestorConfig:
    if source_id == "deepseek":
        return IngestorConfig(
            mode="cookies",
            sync_interval_ms=parse_duration_ms("2m"),
            min_sync_interval_ms=parse_duration_ms("2m"),
            watch=False,
        )
    if source_id in WEB_INGESTORS:
        return IngestorConfig(
            mode="cookies",
            sync_interval_ms=parse_duration_ms("1m"),
            min_sync_interval_ms=parse_duration_ms("1m"),
            watch=False,
        )
    return IngestorConfig(
        sync_interval_ms=parse_duration_ms("1s"),
        min_sync_interval_ms=parse_duration_ms("1s"),
        watch=True,
    )


def read_config_text(path: Path | None = None) -> str:
    path = path or config_path()
    ensure_config(path)
    return path.read_text()


def write_config_text(text: str, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


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
        "ingestors": {
            source_id: _default_ingestor_table(source_id)
            for source_id in INGESTOR_ORDER
        }
    }


def _default_ingestor_table(source_id: str) -> dict[str, Any]:
    config = default_ingestor_config(source_id)
    row: dict[str, Any] = {"enabled": False}
    if config.mode:
        row["mode"] = config.mode
    if config.sync_interval_ms:
        row["sync_interval"] = format_duration_ms(config.sync_interval_ms)
    if config.min_sync_interval_ms:
        row["min_sync_interval"] = format_duration_ms(config.min_sync_interval_ms)
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
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
    ]


def _write_raw_config(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_toml(data))


def _toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in data.items():
        if key == "ingestors" or isinstance(value, dict):
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    if lines:
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
