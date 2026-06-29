"""Spawn a program under a pseudo-tty for tests.

Replaces pty.fork(), which warns under Python 3.14+ and can deadlock
when the parent is multi-threaded (pytest loads onnxruntime/textual).
Uses pty.openpty() + subprocess so the child execs a fresh program
(fork+exec is safe; only fork-without-exec warns), and acquires the
pty as controlling terminal so child pagers can open /dev/tty.
"""

from __future__ import annotations

import fcntl
import os
import pty
import struct
import subprocess
import termios
from typing import Sequence


def _acquire_controlling_tty() -> None:
    # start_new_session already ran setsid(); claim the slave (now fd 0)
    # as controlling terminal so child pagers can open /dev/tty.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def spawn_pty(
    argv: Sequence[str],
    env: dict[str, str] | None = None,
    *,
    width: int = 100,
    height: int = 30,
) -> tuple[subprocess.Popen, int]:
    """Run argv with stdio attached to a pty.

    Returns (proc, master_fd). Caller drives master_fd and must close it.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
    proc = subprocess.Popen(
        list(argv),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
        preexec_fn=_acquire_controlling_tty,
    )
    os.close(slave)
    return proc, master
