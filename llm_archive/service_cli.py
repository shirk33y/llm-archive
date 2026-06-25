from __future__ import annotations

import click

from llm_archive.sync import _run, _sync_one


@click.command(help="Run the scheduler in the foreground (used by service units).")
def service() -> None:
    from llm_archive.service import run_service

    async def runner(src: str, job_force: bool) -> bool:
        return await _sync_one(src, None, None, job_force, None)

    _run(run_service(runner=runner))
