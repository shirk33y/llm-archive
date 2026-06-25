from __future__ import annotations

import click
from rich.console import Console

from llm_archive.sync import _run, _sync_one

console = Console()


@click.group(
    invoke_without_command=True,
    help="Manage scheduler service, or run it in foreground.",
)
@click.pass_context
def service(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _service_foreground()


def _service_foreground() -> None:
    from llm_archive.service import run_service

    async def runner(src: str, job_force: bool) -> bool:
        return await _sync_one(src, None, None, job_force, None)

    _run(run_service(runner=runner))


@service.command("run", help="Run scheduler process in foreground.")
def service_run() -> None:
    _service_foreground()


@service.command("install", help="Install/register scheduler service.")
def service_install() -> None:
    from llm_archive.service_control import install_service

    console.print(install_service())


@service.command("start", help="Start scheduler service.")
def service_start() -> None:
    from llm_archive.service_control import start_service

    console.print(start_service())


@service.command("stop", help="Stop scheduler service.")
def service_stop() -> None:
    from llm_archive.service_control import stop_service

    console.print(stop_service())


@service.command("restart", help="Restart scheduler service.")
def service_restart() -> None:
    from llm_archive.service_control import restart_service

    console.print(restart_service())


@service.command("uninstall", help="Unregister and remove scheduler service.")
def service_uninstall() -> None:
    from llm_archive.service_control import uninstall_service

    console.print(uninstall_service())


@service.command("status", help="Show native service-manager status.")
def service_status_cmd() -> None:
    from llm_archive.service_control import service_status

    service_status()


@service.command("logs", help="Show scheduler service logs.")
@click.option("-n", "--lines", default=100, type=int, help="Number of log lines")
@click.option("-f", "--follow", is_flag=True, help="Follow logs")
def service_logs_cmd(lines: int, follow: bool) -> None:
    from llm_archive.service_control import service_logs

    service_logs(lines, follow)
