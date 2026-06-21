from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


def sh(*cmd: str, env: dict | None = None) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"{' '.join(cmd)} failed:\n{r.stderr}"
    return r.stdout.strip()


def write_config(home: Path, enabled: bool) -> None:
    cfg = home / ".config" / "llm-archive" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "[ingestors.dummy]\n"
        f'enabled = {"true" if enabled else "false"}\n'
        'sync_interval = "1s"\n'
        'min_sync_interval = "1s"\n'
        "watch = false\n"
    )


def get_status(la: str, env: dict) -> dict:
    return json.loads(sh(la, "status", "--json", env=env))


def check_status(data: dict, mode: str) -> bool:
    svc = data.get("service") or {}
    hb = svc.get("heartbeat_at")
    fresh = bool(hb) and (int(time.time() * 1000) - int(hb)) <= 90000
    if not fresh:
        return False

    sources = data.get("sources") or []
    dummy = next((s for s in sources if s.get("id") == "dummy"), None)
    threads = (dummy or {}).get("thread_count") or 0

    jobs = data.get("jobs") or []
    success = any(
        j.get("source_id") == "dummy" and j.get("status") == "success" for j in jobs
    )

    if mode == "disabled":
        return threads == 0 and not success
    if mode == "enabled":
        return success and threads >= 1
    return False


def poll_until(
    la: str, env: dict, mode: str, timeout: int = 120, interval: float = 2
) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            last = get_status(la, env)
            if check_status(last, mode):
                return last
        except Exception:
            pass
        time.sleep(interval)
    pytest.fail(
        f"Timed out waiting for mode={mode}.\nLast status:\n{json.dumps(last, indent=2)}"
    )


@pytest.fixture
def la_venv() -> str:
    return str(Path(__file__).parent.parent / ".venv" / "bin" / "llm-archive")


@pytest.fixture
def e2e_env(tmp_path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    return env


@pytest.fixture
def service_proc(la_venv: str, e2e_env: dict, tmp_path: Path):
    write_config(tmp_path, enabled=False)
    log_path = tmp_path / "svc.log"
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [la_venv, "service"],
        env=e2e_env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    time.sleep(3)
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_file.close()


@pytest.mark.e2e
class TestServiceE2E:
    """E2e: scheduler service with dummy provider (uv-based)."""

    def test_full_flow(self, la_venv: str, e2e_env: dict, service_proc, tmp_path):
        # dummy not in provider catalog
        data = get_status(la_venv, e2e_env)
        ids = [s["id"] for s in data.get("sources", [])]
        assert "dummy" not in ids

        # Phase 1: disabled — heartbeat but no sync
        poll_until(la_venv, e2e_env, "disabled", timeout=40)

        # Phase 2: enable — config hot-reload triggers sync
        write_config(tmp_path, enabled=True)
        poll_until(la_venv, e2e_env, "enabled", timeout=120)

        # Search finds canary
        result = json.loads(
            sh(la_venv, "search", "dummycanarytoken", "--json", env=e2e_env)
        )
        assert result["count"] > 0

        # Logs contain dummy
        logs = sh(la_venv, "logs", env=e2e_env)
        assert "dummy" in logs

        # Embed + semantic search
        output = sh(la_venv, "embed", "--force", env=e2e_env)
        assert "embedded" in output
        result = json.loads(
            sh(la_venv, "search", "-s", "test query", "--json", env=e2e_env)
        )
        assert result["count"] > 0


@pytest.mark.e2e
@pytest.mark.brew
@pytest.mark.skipif(
    not (os.environ.get("CI") and shutil.which("brew")),
    reason="CI + Homebrew required",
)
class TestBrewE2E:
    """E2e: brew-installed llm-archive with brew services (CI only)."""

    def test_full_flow(self):
        la = shutil.which("llm-archive")
        assert la, "llm-archive not in PATH"
        env = os.environ.copy()

        # dummy not in catalog
        data = get_status(la, env)
        ids = [s["id"] for s in data.get("sources", [])]
        assert "dummy" not in ids

        # Write config + start via brew services
        write_config(Path.home(), enabled=True)

        try:
            sh("brew", "services", "start", "llm-archive", env=env)
            time.sleep(3)
            poll_until(la, env, "enabled", timeout=180)

            result = json.loads(
                sh(la, "search", "dummycanarytoken", "--json", env=env)
            )
            assert result["count"] > 0

            output = sh(la, "embed", "--force", env=env)
            assert "embedded" in output
            result = json.loads(
                sh(la, "search", "-s", "test query", "--json", env=env)
            )
            assert result["count"] > 0
        finally:
            subprocess.run(
                ["brew", "services", "stop", "llm-archive"],
                capture_output=True,
            )
