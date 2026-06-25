from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "llm-archive"
SYSTEMD_UNIT = f"{SERVICE_NAME}.service"
LAUNCHD_LABEL = "com.shirk33y.llm-archive"
BREW_FORMULA = "llm-archive"


@dataclass(frozen=True, slots=True)
class ServicePaths:
    unit: Path
    log: Path


def executable_path() -> Path:
    return Path(sys.argv[0]).resolve()


def is_brew_install(executable: Path | None = None) -> bool:
    path = executable or executable_path()
    parts = set(path.parts)
    return "Cellar" in parts and BREW_FORMULA in parts


def is_service_installed(executable: Path | None = None) -> bool:
    """True when a service unit/plist is registered for this install."""
    if is_brew_install(executable):
        return _brew_registered()
    match sys.platform:
        case "linux":
            return _systemd_paths().unit.exists()
        case "darwin":
            return _launchd_paths().unit.exists()
        case _:
            return False


def start_service(executable: Path | None = None, *, install: bool = False) -> str:
    """Start the scheduler service. With ``install`` also register it first."""
    if is_brew_install(executable):
        _run(["brew", "services", "start", BREW_FORMULA])
        return "started via brew services"
    match sys.platform:
        case "linux":
            if install:
                _ensure_systemd_unit(executable or executable_path())
            _run(["systemctl", "--user", "start", SYSTEMD_UNIT])
            return "started"
        case "darwin":
            if install:
                _ensure_launchd_plist(executable or executable_path())
            _run(["launchctl", "kickstart", "-k", _launchd_target()])
            return "started"
        case _:
            raise RuntimeError(f"unsupported service platform: {sys.platform}")


def stop_service(executable: Path | None = None, *, uninstall: bool = False) -> str:
    """Stop the scheduler service. With ``uninstall`` also unregister and remove it."""
    if is_brew_install(executable):
        if uninstall:
            _run(["brew", "services", "stop", BREW_FORMULA])
            return "uninstalled via brew services"
        _run(["brew", "services", "kill", BREW_FORMULA])
        return "stopped via brew services"
    match sys.platform:
        case "linux":
            _run(["systemctl", "--user", "stop", SYSTEMD_UNIT], check=False)
            if uninstall:
                _run(["systemctl", "--user", "disable", SYSTEMD_UNIT], check=False)
                paths = _systemd_paths()
                if paths.unit.exists():
                    paths.unit.unlink()
                _run(["systemctl", "--user", "daemon-reload"])
                return f"uninstalled {SYSTEMD_UNIT}"
            return "stopped"
        case "darwin":
            _run(["launchctl", "kill", "TERM", _launchd_target()], check=False)
            if uninstall:
                paths = _launchd_paths()
                _run(["launchctl", "bootout", _launchd_domain(), str(paths.unit)], check=False)
                if paths.unit.exists():
                    paths.unit.unlink()
                return f"uninstalled {LAUNCHD_LABEL}"
            return "stopped"
        case _:
            raise RuntimeError(f"unsupported service platform: {sys.platform}")


def restart_service(executable: Path | None = None) -> str:
    if is_brew_install(executable):
        _run(["brew", "services", "restart", BREW_FORMULA])
        return "restarted via brew services"
    match sys.platform:
        case "linux":
            _run(["systemctl", "--user", "restart", SYSTEMD_UNIT])
            return "restarted"
        case "darwin":
            _run(["launchctl", "kickstart", "-k", _launchd_target()])
            return "restarted"
        case _:
            raise RuntimeError(f"unsupported service platform: {sys.platform}")


def service_logs(lines: int, follow: bool, executable: Path | None = None) -> None:
    if is_brew_install(executable):
        result = subprocess.run(
            ["brew", "services", "info", BREW_FORMULA, "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        log_path = _brew_log_path(result.stdout)
        _tail(log_path, lines, follow)
        return
    match sys.platform:
        case "linux":
            args = ["journalctl", "--user", "-u", SYSTEMD_UNIT, "-n", str(lines)]
            if follow:
                args.append("-f")
            _run(args)
        case "darwin":
            _tail(_launchd_paths().log, lines, follow)
        case _:
            raise RuntimeError(f"unsupported service platform: {sys.platform}")


def _ensure_systemd_unit(executable: Path) -> None:
    paths = _systemd_paths()
    paths.unit.parent.mkdir(parents=True, exist_ok=True)
    paths.unit.write_text(_systemd_unit(executable))
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", SYSTEMD_UNIT])


def _ensure_launchd_plist(executable: Path) -> None:
    paths = _launchd_paths()
    paths.unit.parent.mkdir(parents=True, exist_ok=True)
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    paths.unit.write_bytes(_launchd_plist(executable, paths.log))
    _run(["launchctl", "bootstrap", _launchd_domain(), str(paths.unit)], check=False)
    _run(["launchctl", "enable", _launchd_target()])


def _brew_registered() -> bool:
    try:
        result = subprocess.run(
            ["brew", "services", "info", BREW_FORMULA, "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return bool(data)


def _systemd_paths() -> ServicePaths:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return ServicePaths(
        unit=config_home / "systemd" / "user" / SYSTEMD_UNIT,
        log=state_home / SERVICE_NAME / "service.log",
    )


def _launchd_paths() -> ServicePaths:
    return ServicePaths(
        unit=Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
        log=Path.home() / "Library" / "Logs" / f"{SERVICE_NAME}.log",
    )


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_target() -> str:
    return f"{_launchd_domain()}/{LAUNCHD_LABEL}"


def _systemd_unit(executable: Path) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=llm-archive scheduler",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={executable} service",
            "Restart=always",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def _launchd_plist(executable: Path, log_path: Path) -> bytes:
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(executable), "service"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(payload, sort_keys=True)


def _brew_log_path(stdout: str) -> Path:
    data = json.loads(stdout)
    if data and isinstance(data, list):
        item = data[0]
        log_path = item.get("log_path") or item.get("error_log_path")
        if isinstance(log_path, str) and log_path:
            return Path(log_path)
    raise RuntimeError("brew services did not report a log path")


def _tail(path: Path, lines: int, follow: bool) -> None:
    if not path.exists():
        raise RuntimeError(f"log not found: {path}")
    args = ["tail", "-n", str(lines), str(path)]
    if follow:
        args.insert(1, "-f")
    _run(args)


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    if not shutil.which(args[0]):
        raise RuntimeError(f"command not found: {args[0]}")
    return subprocess.run(args, check=check, text=True)
