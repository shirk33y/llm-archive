from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from llm_archive import cli, service_control


def _brew_executable(tmp_path: Path) -> Path:
    """Synthetic brew Cellar path: encodes detection signal without a real prefix/version."""
    return tmp_path / "Cellar" / "llm-archive" / "bin" / "llm-archive"


def _fake_run_factory(calls):
    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    return fake_run


def test_is_brew_install_synthetic_detection(tmp_path):
    assert service_control.is_brew_install(_brew_executable(tmp_path))
    assert not service_control.is_brew_install(tmp_path / ".venv" / "bin" / "llm-archive")


def test_is_service_installed_native_absent_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert not service_control.is_service_installed(tmp_path / "repo" / "bin" / "llm-archive")


def test_is_service_installed_native_present_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / "config"
    unit = config_home / "systemd" / "user" / "llm-archive.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    assert service_control.is_service_installed(tmp_path / "repo" / "bin" / "llm-archive")


def test_is_service_installed_brew_registered(monkeypatch, tmp_path):
    path = _brew_executable(tmp_path)

    def fake_subprocess_run(args, **kwargs):
        assert args == ["brew", "services", "info", "llm-archive", "--json"]
        return subprocess.CompletedProcess(args, 0, stdout='[{"name":"llm-archive"}]')

    monkeypatch.setattr(service_control.subprocess, "run", fake_subprocess_run)
    assert service_control.is_service_installed(path)


def test_is_service_installed_brew_not_registered(monkeypatch, tmp_path):
    path = _brew_executable(tmp_path)

    def fake_subprocess_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="[]")

    monkeypatch.setattr(service_control.subprocess, "run", fake_subprocess_run)
    assert not service_control.is_service_installed(path)


def test_brew_start_registers_and_starts(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(service_control, "_run", _fake_run_factory(calls))

    assert service_control.start_service(_brew_executable(tmp_path)) == "started via brew services"
    assert service_control.start_service(_brew_executable(tmp_path), install=True) == "started via brew services"
    assert calls == [["brew", "services", "start", "llm-archive"]] * 2


def test_brew_stop_kill_vs_uninstall(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(service_control, "_run", _fake_run_factory(calls))
    path = _brew_executable(tmp_path)

    assert service_control.stop_service(path) == "stopped via brew services"
    assert service_control.stop_service(path, uninstall=True) == "uninstalled via brew services"
    assert calls == [
        ["brew", "services", "kill", "llm-archive"],
        ["brew", "services", "stop", "llm-archive"],
    ]


def test_brew_restart(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(service_control, "_run", _fake_run_factory(calls))
    path = _brew_executable(tmp_path)

    assert service_control.restart_service(path) == "restarted via brew services"
    assert calls == [["brew", "services", "restart", "llm-archive"]]


def test_systemd_start_install_writes_enables_starts(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    executable = tmp_path / "bin" / "llm-archive"
    config_home = tmp_path / "config"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(service_control, "_run", _fake_run_factory(calls))

    assert service_control.start_service(executable, install=True) == "started"

    unit = config_home / "systemd" / "user" / "llm-archive.service"
    assert f"ExecStart={executable} service" in unit.read_text()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "llm-archive.service"],
        ["systemctl", "--user", "start", "llm-archive.service"],
    ]


def test_systemd_start_without_install_only_starts(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service_control, "_run", _fake_run_factory(calls))

    msg = service_control.start_service(Path("/repo/.venv/bin/llm-archive"), install=False)
    assert msg == "started"
    assert calls == [["systemctl", "--user", "start", "llm-archive.service"]]


def test_systemd_stop_and_uninstall(monkeypatch, tmp_path):
    calls: list[tuple[list[str], bool]] = []
    config_home = tmp_path / "config"
    unit = config_home / "systemd" / "user" / "llm-archive.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    def fake_run(args, *, check=True):
        calls.append((args, check))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(service_control, "_run", fake_run)

    assert service_control.stop_service(Path("/repo/.venv/bin/llm-archive")) == "stopped"
    assert calls == [(["systemctl", "--user", "stop", "llm-archive.service"], False)]

    assert (
        service_control.stop_service(Path("/repo/.venv/bin/llm-archive"), uninstall=True)
        == "uninstalled llm-archive.service"
    )
    assert not unit.exists()
    assert calls[1:] == [
        (["systemctl", "--user", "stop", "llm-archive.service"], False),
        (["systemctl", "--user", "disable", "llm-archive.service"], False),
        (["systemctl", "--user", "daemon-reload"], True),
    ]


def test_systemd_restart(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service_control, "_run", _fake_run_factory(calls))

    assert service_control.restart_service(Path("/repo/.venv/bin/llm-archive")) == "restarted"
    assert calls == [["systemctl", "--user", "restart", "llm-archive.service"]]


def test_launchd_start_install_writes_plist_and_starts(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    executable = tmp_path / "bin" / "llm-archive"
    monkeypatch.setattr(service_control, "_run", _fake_run_factory(calls))

    assert service_control.start_service(executable, install=True) == "started"

    plist = tmp_path / "Library" / "LaunchAgents" / "com.shirk33y.llm-archive.plist"
    assert plist.exists()
    domain = f"gui/{os.getuid()}"
    assert calls == [
        ["launchctl", "bootstrap", domain, str(plist)],
        ["launchctl", "enable", f"{domain}/com.shirk33y.llm-archive"],
        ["launchctl", "kickstart", "-k", f"{domain}/com.shirk33y.llm-archive"],
    ]


def test_launchd_stop_uninstall_removes_plist(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.shirk33y.llm-archive.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"<plist/>")

    def fake_run(args, *, check=True):
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(service_control, "_run", fake_run)

    assert (
        service_control.stop_service(Path("/repo/.venv/bin/llm-archive"), uninstall=True)
        == "uninstalled com.shirk33y.llm-archive"
    )
    assert not plist.exists()


def test_service_logs_brew_uses_reported_log_path(monkeypatch, tmp_path):
    path = _brew_executable(tmp_path)
    tails: list[tuple[Path, int, bool]] = []

    def fake_subprocess_run(args, **kwargs):
        assert args == ["brew", "services", "info", "llm-archive", "--json"]
        return subprocess.CompletedProcess(args, 0, stdout='[{"log_path":"/tmp/la.log"}]')

    monkeypatch.setattr(service_control.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(service_control, "_tail", lambda p, n, f: tails.append((p, n, f)))

    service_control.service_logs(25, True, path)
    assert tails == [(Path("/tmp/la.log"), 25, True)]


def test_service_logs_linux_uses_journalctl(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service_control, "_run", _fake_run_factory(calls))

    service_control.service_logs(10, True, Path("/repo/.venv/bin/llm-archive"))
    assert calls == [["journalctl", "--user", "-u", "llm-archive.service", "-n", "10", "-f"]]


def test_brew_log_path_rejects_missing_log():
    with pytest.raises(RuntimeError, match="log path"):
        service_control._brew_log_path('[{"name":"llm-archive"}]')


def test_cli_start_when_uninstalled_hints(monkeypatch):
    monkeypatch.setattr(service_control, "is_service_installed", lambda executable=None: False)
    result = CliRunner().invoke(cli.main, ["start"])

    assert result.exit_code == 1
    assert "start --install" in result.output


def test_cli_start_install_calls_start_service(monkeypatch):
    monkeypatch.setattr(service_control, "is_service_installed", lambda executable=None: False)
    monkeypatch.setattr(
        service_control, "start_service", lambda executable=None, *, install=False: "started"
    )
    result = CliRunner().invoke(cli.main, ["start", "--install"])

    assert result.exit_code == 0
    assert "started" in result.output


def test_cli_start_when_installed_starts(monkeypatch):
    monkeypatch.setattr(service_control, "is_service_installed", lambda executable=None: True)
    monkeypatch.setattr(
        service_control, "start_service", lambda executable=None, *, install=False: "started"
    )
    result = CliRunner().invoke(cli.main, ["start"])

    assert result.exit_code == 0


def test_cli_stop_uninstall_and_restart(monkeypatch):
    monkeypatch.setattr(
        service_control, "stop_service", lambda executable=None, *, uninstall=False: "uninstalled x"
    )
    monkeypatch.setattr(service_control, "restart_service", lambda executable=None: "restarted x")

    stop_res = CliRunner().invoke(cli.main, ["stop", "--uninstall"])
    restart_res = CliRunner().invoke(cli.main, ["restart"])

    assert stop_res.exit_code == 0
    assert "uninstalled x" in stop_res.output
    assert restart_res.exit_code == 0
    assert "restarted" in restart_res.output


def test_cli_logs_invokes_service_logs(monkeypatch):
    seen: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        service_control, "service_logs", lambda lines, follow, executable=None: seen.append((lines, follow))
    )
    result = CliRunner().invoke(cli.main, ["logs", "-n", "5", "-f"])

    assert result.exit_code == 0
    assert seen == [(5, True)]


def test_service_command_runs_foreground_runner():
    result = CliRunner().invoke(cli.main, ["service", "--help"])
    assert result.exit_code == 0
    assert "scheduler" in result.output.lower()
