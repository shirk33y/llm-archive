from __future__ import annotations

import os
import sys
from pathlib import Path

from llm_archive.config import AppConfig, config_path


class DevMode:
    def __init__(self, paths: list[Path], debounce_ms: int, gate_command: str | None, reload):
        self._paths = paths
        self._debounce_s = debounce_ms / 1000
        self._gate_command = gate_command
        self._reload = reload
        self._mtimes: dict[Path, float] = {}
        self._last_trigger = 0.0

    @classmethod
    def from_config(cls, config: AppConfig, *, reload) -> DevMode | None:
        if not config.dev.watch:
            return None
        paths = [config_path().resolve()]
        pkg_dir = Path(__file__).resolve().parent
        if pkg_dir.is_dir():
            paths.append(pkg_dir)
        for extra in config.dev.watch_paths:
            p = Path(extra).resolve()
            if p.exists():
                paths.append(p)
        return cls(paths, config.dev.debounce_ms, config.dev.gate_command, reload) if paths else None

    def start(self) -> None:
        self._snapshot()

    def stop(self) -> None:
        pass

    def reload(self) -> None:
        self._reload()

    def poll(self) -> bool:
        for path in self._paths:
            mtime = self._mtime(path)
            if mtime is None:
                continue
            prev = self._mtimes.get(path)
            if prev is not None and mtime > prev:
                self._mtimes[path] = mtime
                return True
        return False

    def _snapshot(self) -> None:
        for path in self._paths:
            mtime = self._mtime(path)
            if mtime is not None:
                self._mtimes[path] = mtime

    def _mtime(self, path: Path) -> float | None:
        if path.is_file():
            return path.stat().st_mtime
        if path.is_dir():
            return max(
                (child.stat().st_mtime for child in path.rglob("*") if child.is_file()),
                default=None,
            )
        return None


def reexec() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])
