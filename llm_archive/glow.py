from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

BACKEND: str | None = None
BACKEND_PATH: str | None = None
BACKEND_CHECKED = False

# mdcat streams to pager (linear, ~0.3s/MB, first paint ~33ms regardless of
# size). glow renders the whole file first (super-linear: ~0.4s at 1MB,
# ~20s at 7.4MB). Prefer mdcat; fall back to glow; fall back to less -R.

# glow's styled reflow is super-linear. Above this size callers using glow
# should skip it and page the raw markdown. mdcat is exempt (linear).
MAX_SIZE = 1_000_000


def preferred() -> str | None:
    """Return best available markdown viewer backend name."""
    global BACKEND, BACKEND_PATH, BACKEND_CHECKED
    if BACKEND_CHECKED:
        return BACKEND
    BACKEND_CHECKED = True
    for name in ("mdcat", "glow"):
        p = shutil.which(name)
        if p:
            BACKEND = name
            BACKEND_PATH = p
            return BACKEND
    return None


def is_available() -> bool:
    return preferred() is not None


def is_too_large(path: Path) -> bool:
    # mdcat is linear and streams, so size is not a concern. Only glow needs
    # this guard. When mdcat is active we never skip rendering.
    if preferred() == "mdcat":
        return False
    try:
        return path.stat().st_size > MAX_SIZE
    except OSError:
        return False


def view(path: Path, width: int = 0) -> int:
    backend = preferred()
    if backend == "mdcat":
        cmd = [BACKEND_PATH, "-p"]
        if width:
            cmd.extend(["--columns", str(width)])
        cmd.append(str(path))
        return subprocess.run(cmd).returncode
    if backend == "glow":
        cmd = [BACKEND_PATH, "-p", "-s", "auto"]
        if width:
            cmd.extend(["-w", str(width)])
        cmd.append(str(path))
        return subprocess.run(cmd).returncode
    pager = shutil.which("less")
    if pager:
        return subprocess.run([pager, "-R", str(path)]).returncode
    raise RuntimeError("no markdown viewer available (mdcat, glow, or less)")
