from __future__ import annotations

from contextlib import suppress
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import pytest


def sh(*cmd: str, env: dict | None = None) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"{' '.join(cmd)} failed:\n{r.stderr}"
    return r.stdout.strip()


def write_config(cfg: Path, enabled: bool) -> None:
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "[ingestors.dummy]\n"
        f"enabled = {'true' if enabled else 'false'}\n"
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

    jobs = data.get("jobs") or []
    success = any(j.get("source_id") == "dummy" and j.get("status") == "success" for j in jobs)

    if mode == "disabled":
        return not success
    if mode == "enabled":
        return success
    return False


def poll_until(la: str, env: dict, mode: str, timeout: int = 120, interval: float = 2) -> dict:
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
    pytest.fail(f"Timed out waiting for mode={mode}.\nLast status:\n{json.dumps(last, indent=2)}")


@pytest.fixture
def la_venv() -> str:
    return str(Path(__file__).parent.parent / ".venv" / "bin" / "llm-archive")


@pytest.fixture
def e2e_env(tmp_path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["LLM_ARCHIVE_ENABLE_TEST_SOURCES"] = "1"
    return env


@pytest.fixture
def service_proc(la_venv: str, e2e_env: dict, tmp_path: Path):
    cfg = Path(
        os.environ.get(
            "LLM_ARCHIVE_CONFIG", str(tmp_path / ".config" / "llm-archive" / "config.toml")
        )
    )
    write_config(cfg, enabled=True)
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
        try:
            poll_until(la_venv, e2e_env, "enabled", timeout=120)

            # Search finds canary
            result = json.loads(sh(la_venv, "search", "dummycanarytoken", "--json", env=e2e_env))
            assert result["count"] > 0

            # Embed + semantic search
            output = sh(la_venv, "embed", "--force", env=e2e_env)
            assert "embedded" in output
            result = json.loads(sh(la_venv, "search", "-s", "test query", "--json", env=e2e_env))
            assert result["count"] > 0
        except Exception:
            log = tmp_path / "svc.log"
            if log.exists():
                print(f"\n--- service log ({log}) ---")
                print(log.read_text())
                print("--- end service log ---")
            raise


@pytest.mark.e2e
@pytest.mark.brew
@pytest.mark.skipif(
    not (os.environ.get("CI") and shutil.which("brew")),
    reason="CI + Homebrew required",
)
class TestBrewE2E:
    """E2e: brew-installed llm-archive with brew services (CI only)."""

    def test_full_flow(self, tmp_path: Path):
        la = shutil.which("llm-archive")
        assert la, "llm-archive not in PATH"
        config_path = tmp_path / "config.toml"
        db_path = tmp_path / "archive.db"
        env = os.environ.copy()
        env["LLM_ARCHIVE_ENABLE_TEST_SOURCES"] = "1"
        env["LLM_ARCHIVE_CONFIG"] = str(config_path)
        env["LLM_ARCHIVE_DB"] = str(db_path)

        # dummy not in catalog
        data = get_status(la, env)
        ids = [s["id"] for s in data.get("sources", [])]
        assert "dummy" not in ids

        # Write config + propagate env to service manager + start via brew services
        write_config(config_path, enabled=True)

        try:
            sh("brew", "services", "start", "llm-archive", env=env)
            time.sleep(2)

            # debug: check service state
            if platform.system() == "Linux":
                print(sh("systemctl", "--user", "status", "homebrew.llm-archive"))
                print(sh("systemctl", "--user", "show", "--property=Environment", "homebrew.llm-archive"))

            poll_until(la, env, "enabled", timeout=30)

            result = json.loads(sh(la, "search", "dummycanarytoken", "--json", env=env))
            assert result["count"] > 0

            output = sh(la, "embed", "--force", env=env)
            assert "embedded" in output
            result = json.loads(sh(la, "search", "-s", "test query", "--json", env=env))
            assert result["count"] > 0
        finally:
            with suppress(AssertionError):
                sh("brew", "services", "stop", "llm-archive")
