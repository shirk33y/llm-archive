"""Tests for markdown viewer backend integration.

The viewer layer abstracts over mdcat (preferred, streaming) and glow
(fallback, full-render). mdcat streams to a pager with linear scaling;
glow renders the whole file first and is super-linear, so is_too_large
guards only the glow path.

Regression guard: the backend must be launched in pager mode (-p) or it
exits immediately, dumping the user back to the thread list instead of
an interactive viewer that stays open until quit.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import time

import pytest

from llm_archive import glow
from tests._pty import spawn_pty


def _reset_backend(monkeypatch):
    """Reset cached backend detection."""
    monkeypatch.setattr(glow, "BACKEND", None)
    monkeypatch.setattr(glow, "BACKEND_PATH", None)
    monkeypatch.setattr(glow, "BACKEND_CHECKED", False)


def _capture(monkeypatch, tmp_path, *, backend: str, size: int = 128, width: int = 100, rc: int = 0):
    _reset_backend(monkeypatch)
    monkeypatch.setattr(glow, "BACKEND", backend)
    monkeypatch.setattr(glow, "BACKEND_PATH", f"/usr/bin/{backend}")
    monkeypatch.setattr(glow, "BACKEND_CHECKED", True)
    md = tmp_path / "thread.md"
    md.write_bytes(b"x" * size)
    seen: dict = {}

    class _Result:
        def __init__(self, code):
            self.returncode = code

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _Result(rc)

    monkeypatch.setattr(glow.subprocess, "run", fake_run)
    returned = glow.view(md, width=width)
    return seen.get("cmd", []), returned


# ── glow backend ──────────────────────────────────────────────────────────────


def test_glow_view_invokes_pager_mode(monkeypatch, tmp_path):
    cmd, _ = _capture(monkeypatch, tmp_path, backend="glow")
    assert cmd[0] == "/usr/bin/glow"
    assert "-p" in cmd, f"glow must run in pager mode: {cmd}"


def test_glow_small_file_uses_styled_reflow(monkeypatch, tmp_path):
    cmd, _ = _capture(monkeypatch, tmp_path, backend="glow", size=128)
    assert cmd[cmd.index("-s") + 1] == "auto"
    assert "-w" in cmd and cmd[cmd.index("-w") + 1] == "100", f"styled mode should pass width: {cmd}"


def test_glow_zero_width_omits_w_flag(monkeypatch, tmp_path):
    cmd, _ = _capture(monkeypatch, tmp_path, backend="glow", size=128, width=0)
    assert cmd[cmd.index("-s") + 1] == "auto"
    assert "-w" not in cmd, f"width=0 must not pass -w: {cmd}"


# ── mdcat backend ─────────────────────────────────────────────────────────────


def test_mdcat_view_invokes_pager_mode(monkeypatch, tmp_path):
    cmd, _ = _capture(monkeypatch, tmp_path, backend="mdcat")
    assert cmd[0] == "/usr/bin/mdcat"
    assert "-p" in cmd, f"mdcat must run in pager mode: {cmd}"


def test_mdcat_passes_columns_flag(monkeypatch, tmp_path):
    cmd, _ = _capture(monkeypatch, tmp_path, backend="mdcat", width=100)
    assert "--columns" in cmd
    assert cmd[cmd.index("--columns") + 1] == "100"


def test_mdcat_zero_width_omits_columns(monkeypatch, tmp_path):
    cmd, _ = _capture(monkeypatch, tmp_path, backend="mdcat", width=0)
    assert "--columns" not in cmd


def test_mdcat_is_too_large_always_false(monkeypatch, tmp_path):
    _reset_backend(monkeypatch)
    monkeypatch.setattr(glow, "BACKEND", "mdcat")
    monkeypatch.setattr(glow, "BACKEND_CHECKED", True)
    md = tmp_path / "big.md"
    md.write_bytes(b"x" * (glow.MAX_SIZE * 10))
    assert glow.is_too_large(md) is False


# ── shared ────────────────────────────────────────────────────────────────────


def test_view_propagates_returncode(monkeypatch, tmp_path):
    _, returned = _capture(monkeypatch, tmp_path, backend="glow", rc=42)
    assert returned == 42


def test_view_raises_when_no_backend(monkeypatch, tmp_path):
    _reset_backend(monkeypatch)
    monkeypatch.setattr(glow, "BACKEND", None)
    monkeypatch.setattr(glow, "BACKEND_CHECKED", True)
    monkeypatch.setattr(glow.shutil, "which", lambda _n: None)
    md = tmp_path / "thread.md"
    md.write_text("body")
    with pytest.raises(RuntimeError, match="no markdown viewer available"):
        glow.view(md, width=100)


def test_is_too_large_boundary_glow(monkeypatch, tmp_path):
    _reset_backend(monkeypatch)
    monkeypatch.setattr(glow, "BACKEND", "glow")
    monkeypatch.setattr(glow, "BACKEND_CHECKED", True)
    md = tmp_path / "t.md"
    md.write_bytes(b"x" * glow.MAX_SIZE)
    assert glow.is_too_large(md) is False
    md.write_bytes(b"x" * (glow.MAX_SIZE + 1))
    assert glow.is_too_large(md) is True


def test_is_too_large_missing_file(monkeypatch, tmp_path):
    _reset_backend(monkeypatch)
    monkeypatch.setattr(glow, "BACKEND", "glow")
    monkeypatch.setattr(glow, "BACKEND_CHECKED", True)
    assert glow.is_too_large(tmp_path / "nope.md") is False


def test_preferred_prefers_mdcat_over_glow(monkeypatch):
    _reset_backend(monkeypatch)
    monkeypatch.setattr(glow.shutil, "which", lambda name: f"/usr/bin/{name}" if name in ("mdcat", "glow") else None)
    assert glow.preferred() == "mdcat"


def test_preferred_falls_back_to_glow(monkeypatch):
    _reset_backend(monkeypatch)
    monkeypatch.setattr(glow.shutil, "which", lambda name: "/usr/bin/glow" if name == "glow" else None)
    assert glow.preferred() == "glow"


def test_is_available_caches_path_lookup(monkeypatch):
    _reset_backend(monkeypatch)
    calls = {"n": 0}

    def fake_which(_name):
        calls["n"] += 1
        return "/usr/bin/glow"

    monkeypatch.setattr(glow.shutil, "which", fake_which)
    assert glow.is_available()
    assert glow.is_available()
    assert calls["n"] == 1, "shutil.which must run only once (cached)"


# ── real backend tests ────────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("glow") is None, reason="glow not installed")
def test_real_glow_launches_and_renders(tmp_path):
    md = tmp_path / "thread.md"
    md.write_text("# Thread\n\n" + "body line of content\n" * 6)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    proc, master = spawn_pty(
        [shutil.which("glow"), "-p", "-s", "auto", "-w", "100", str(md)],
        env=env,
    )

    saw_output = False
    try:
        start = time.monotonic()
        while time.monotonic() - start < 0.8:
            readable, _, _ = select.select([master], [], [], 0.05)
            if master in readable:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                saw_output = True
                s = data.decode("utf-8", "replace")
                if "\x1b]11;?" in s:
                    os.write(master, b"\x1b]11;rgb:0000/0000\x1b\\")
                if "\x1b[6n" in s:
                    os.write(master, b"\x1b[1;1R")
    finally:
        try:
            os.write(master, b"q")
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)

    assert saw_output, "glow produced no terminal output"
    assert proc.returncode == 0, f"glow exited with {proc.returncode}"


@pytest.mark.skipif(shutil.which("mdcat") is None, reason="mdcat not installed")
def test_real_mdcat_launches_and_renders(tmp_path):
    md = tmp_path / "thread.md"
    md.write_text("# Thread\n\n" + "body line of content\n" * 6)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    proc, master = spawn_pty(
        [shutil.which("mdcat"), "-p", str(md)],
        env=env,
    )

    saw_output = False
    try:
        start = time.monotonic()
        while time.monotonic() - start < 2.0:
            readable, _, _ = select.select([master], [], [], 0.05)
            if master in readable:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                saw_output = True
                s = data.decode("utf-8", "replace")
                if "\x1b[6n" in s:
                    os.write(master, b"\x1b[1;1R")
    finally:
        try:
            os.write(master, b"q")
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)

    assert saw_output, "mdcat produced no terminal output"


@pytest.mark.skipif(shutil.which("mdcat") is None, reason="mdcat not installed")
def test_mdcat_large_file_first_paint_fast(tmp_path):
    """mdcat must stream: first paint should be near-instant even for large files."""
    md = tmp_path / "large.md"
    unit = "# heading\n\n" + "word " * 100 + "\n\n"
    md.write_text(unit * 5000)
    assert md.stat().st_size > 1_000_000

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    proc, master = spawn_pty(
        [shutil.which("mdcat"), "-p", str(md)],
        env=env,
    )

    first_paint = None
    try:
        start = time.monotonic()
        while time.monotonic() - start < 5.0:
            readable, _, _ = select.select([master], [], [], 0.02)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if data:
                    if first_paint is None:
                        first_paint = time.monotonic() - start
                    if len(data) > 100:
                        break
                    s = data.decode("utf-8", "replace")
                    if "\x1b[6n" in s:
                        os.write(master, b"\x1b[1;1R")
    finally:
        try:
            os.write(master, b"q")
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)

    assert first_paint is not None, "mdcat produced no output"
    assert first_paint < 0.5, f"mdcat first paint {first_paint:.3f}s (expected <0.5s for streaming)"


@pytest.mark.skipif(shutil.which("glow") is None, reason="glow not installed")
def test_real_glow_renders_at_max_size_fast(tmp_path):
    # Guard that MAX_SIZE stays in glow's fast zone: render a file at the
    # threshold with -s auto; must finish in well under a second (~0.4s).
    md = tmp_path / "at_max.md"
    buf = ["# title\n"]
    written = len(buf[0])
    unit = len("word " * 24 + "\n")
    while written + unit < glow.MAX_SIZE:
        buf.append("word " * 24 + "\n")
        written += unit
    md.write_text("".join(buf))
    assert md.stat().st_size < glow.MAX_SIZE, "test file must be under MAX_SIZE"

    t0 = time.monotonic()
    r = subprocess.run(["glow", "-s", "auto", str(md)], capture_output=True)
    elapsed = time.monotonic() - t0

    assert r.returncode == 0
    assert elapsed < 3.0, f"glow auto render at MAX_SIZE took {elapsed:.2f}s"
