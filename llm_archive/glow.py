from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_GLOW_PATH: str | None = None
_GLOW_CHECKED = False

# glow's styled reflow is super-linear: ~0.4s at 1MB, ~20s at 7.4MB. Above
# this size callers should skip glow and page the raw markdown (e.g. less).
MAX_SIZE = 1_000_000


def is_available() -> bool:
    global _GLOW_PATH, _GLOW_CHECKED
    if _GLOW_CHECKED:
        return _GLOW_PATH is not None
    _GLOW_PATH = shutil.which("glow")
    _GLOW_CHECKED = True
    return _GLOW_PATH is not None


def is_too_large(path: Path) -> bool:
    try:
        return path.stat().st_size > MAX_SIZE
    except OSError:
        return False


def view(path: Path, width: int = 0) -> int:
    if not is_available():
        raise RuntimeError("glow not found on PATH")
    cmd = [_GLOW_PATH, "-p", "-s", "auto"]
    if width:
        cmd.extend(["-w", str(width)])
    cmd.append(str(path))
    return subprocess.run(cmd).returncode
