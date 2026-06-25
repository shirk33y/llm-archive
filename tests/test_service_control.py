from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from llm_archive import cli
from llm_archive import service_control


def test_is_brew_install_detects_cellar_path():
    path = Path("/home/linuxbrew/.linuxbrew/Cellar/llm-archive/0.5.0/bin/llm-archive")

    assert service_control.is_brew_install(path)
    assert not service_control.is_brew_install(Path("/repo/.venv/bin/llm-archive"))


def test_brew_install_wraps_brew_services(monkeypatch):
    calls: list[list[str]] = []
    path = Path("/home/linuxbrew/.linuxbrew/Cellar/llm-archive/0.5.0/bin/llm-archive")

    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(service_control, "_run", fake_run)

    message = service_control.install_service(path)

    assert message == "installed via brew services"
    assert calls == [["brew", "services", "start", "llm-archive"]]


def test_brew_stop_uses_kill_to_keep_registration(monkeypatch):
    calls: list[list[str]] = []
    path = Path("/home/linuxbrew/.linuxbrew/Cellar/llm-archive/0.5.0/bin/llm-archive")

    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(service_control, "_run", fake_run)

    message = service_control.stop_service(path)

    assert message == "stopped via brew services"
    assert calls == [["brew", "services", "kill", "llm-archive"]]


def test_brew_start_restart_uninstall_status(monkeypatch):
    calls: list[list[str]] = []
    path = Path("/home/linuxbrew/.linuxbrew/Cellar/llm-archive/0.5.0/bin/llm-archive")

    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(service_control, "_run", fake_run)

    assert service_control.start_service(path) == "started via brew services"
    assert service_control.restart_service(path) == "restarted via brew services"
    assert service_control.uninstall_service(path) == "uninstalled via brew services"
    service_control.service_status(path)

    assert calls == [
        ["brew", "services", "start", "llm-archive"],
        ["brew", "services", "restart", "llm-archive"],
        ["brew", "services", "stop", "llm-archive"],
        ["brew", "services", "info", "llm-archive"],
    ]


def test_systemd_install_writes_unit_and_enables(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    executable = tmp_path / "bin" / "llm-archive"
    config_home = tmp_path / "config"
    state_home = tmp_path / "state"

    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setattr(service_control, "_run", fake_run)

    message = service_control.install_service(executable)

    unit = config_home / "systemd" / "user" / "llm-archive.service"
    assert message == "installed llm-archive.service"
    assert f"ExecStart={executable} service" in unit.read_text()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "llm-archive.service"],
    ]


def test_systemd_lifecycle_commands(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service_control, "_run", fake_run)

    assert service_control.start_service(Path("/repo/.venv/bin/llm-archive")) == "started"
    assert service_control.stop_service(Path("/repo/.venv/bin/llm-archive")) == "stopped"
    assert service_control.restart_service(Path("/repo/.venv/bin/llm-archive")) == "restarted"
    service_control.service_status(Path("/repo/.venv/bin/llm-archive"))

    assert calls == [
        ["systemctl", "--user", "start", "llm-archive.service"],
        ["systemctl", "--user", "stop", "llm-archive.service"],
        ["systemctl", "--user", "restart", "llm-archive.service"],
        ["systemctl", "--user", "status", "llm-archive.service", "--no-pager"],
    ]


def test_systemd_uninstall_removes_unit(monkeypatch, tmp_path):
    calls: list[tuple[list[str], bool]] = []
    config_home = tmp_path / "config"
    unit = config_home / "systemd" / "user" / "llm-archive.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")

    def fake_run(args: list[str], *, check: bool = True):
        calls.append((args, check))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(service_control, "_run", fake_run)

    message = service_control.uninstall_service(Path("/repo/.venv/bin/llm-archive"))

    assert message == "removed llm-archive.service"
    assert not unit.exists()
    assert calls == [
        (["systemctl", "--user", "disable", "--now", "llm-archive.service"], False),
        (["systemctl", "--user", "daemon-reload"], True),
    ]


def test_service_logs_uses_brew_reported_log_path(monkeypatch):
    path = Path("/home/linuxbrew/.linuxbrew/Cellar/llm-archive/0.5.0/bin/llm-archive")
    tails: list[tuple[Path, int, bool]] = []

    def fake_subprocess_run(args: list[str], **kwargs: object):
        assert args == ["brew", "services", "info", "llm-archive", "--json"]
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(args, 0, stdout='[{"log_path":"/tmp/la.log"}]')

    def fake_tail(log_path: Path, lines: int, follow: bool):
        tails.append((log_path, lines, follow))

    monkeypatch.setattr(service_control.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(service_control, "_tail", fake_tail)

    service_control.service_logs(25, True, path)

    assert tails == [(Path("/tmp/la.log"), 25, True)]


def test_service_logs_linux_uses_journalctl(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service_control, "_run", fake_run)

    service_control.service_logs(10, True, Path("/repo/.venv/bin/llm-archive"))

    assert calls == [["journalctl", "--user", "-u", "llm-archive.service", "-n", "10", "-f"]]


def test_brew_log_path_rejects_missing_log():
    with pytest.raises(RuntimeError, match="log path"):
        service_control._brew_log_path('[{"name":"llm-archive"}]')


def test_service_group_lists_subcommands():
    result = CliRunner().invoke(cli.main, ["service", "--help"])

    assert result.exit_code == 0
    assert "install" in result.output
    assert "start" in result.output
    assert "logs" in result.output
