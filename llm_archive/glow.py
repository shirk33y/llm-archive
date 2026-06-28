from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_GLOW_PATH: str | None = None
_GLOW_CHECKED = False


def is_available() -> bool:
    global _GLOW_PATH, _GLOW_CHECKED
    if _GLOW_CHECKED:
        return _GLOW_PATH is not None
    _GLOW_PATH = shutil.which("glow")
    _GLOW_CHECKED = True
    return _GLOW_PATH is not None


def view(path: Path, width: int = 0) -> int:
    if not is_available():
        raise RuntimeError("glow not found on PATH")
    cmd = [_GLOW_PATH, "-s", "auto"]
    if width:
        cmd.extend(["-w", str(width)])
    cmd.append(str(path))
    return subprocess.run(cmd).returncode