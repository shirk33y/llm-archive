from __future__ import annotations

from pathlib import Path

import click

from llm_archive.browser_profiles import BrowserProfile, verified_cookie_profiles
from llm_archive.config import format_duration_ms, update_ingestor_config
from llm_archive.providers import PROVIDERS, provider_kind, provider_paths


def enable_provider(
    source_id: str,
    *,
    browser: str | None = None,
    profile: str | None = None,
    browser_path: str | None = None,
    path: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    kind = provider_kind(source_id)
    if kind == "web":
        values = _enable_web(source_id, browser, profile, browser_path)
    else:
        values = _enable_file(source_id, path)
    values["enabled"] = True
    if not dry_run:
        update_ingestor_config(source_id, values)
    return values


def disable_provider(source_id: str) -> None:
    update_ingestor_config(source_id, {"enabled": False})


def _enable_web(
    source_id: str,
    browser: str | None,
    profile: str | None,
    browser_path: str | None,
) -> dict:
    provider = PROVIDERS[source_id]
    matches = verified_cookie_profiles(provider.domains)
    if browser:
        matches = [item for item in matches if item.browser == browser]
    if profile:
        matches = [item for item in matches if item.profile == profile or str(item.path) == profile]
    chosen = _choose_profile(matches)
    if chosen:
        return {
            "mode": "cookies",
            "browser": chosen.browser,
            "profile": chosen.profile,
            "browser_dir": str(chosen.path),
            "browser_path": browser_path,
        }
    if browser_path:
        return {"mode": "cookies", "browser_path": browser_path}
    raise click.ClickException(
        f"No active {source_id} browser session found. Login in browser or pass --browser-path."
    )


def _enable_file(source_id: str, path: str | None) -> dict:
    if path:
        return {"path": str(Path(path).expanduser())}
    defaults = list(provider_paths(source_id))
    if not defaults:
        if source_id == "windsurf":
            return {}
        raise click.ClickException(f"No data path found for {source_id}. Pass --path.")
    if len(defaults) == 1:
        return {"path": str(defaults[0])}
    existing = [p for p in defaults if p.exists()]
    if not existing:
        return {"path": str(defaults[0])}
    if len(existing) == 1:
        return {"path": str(existing[0])}
    labels = [str(item) for item in existing]
    choice = click.prompt(
        "Choose data path",
        type=click.Choice([str(i + 1) for i in range(len(labels))]),
        show_choices=False,
        default="1",
    )
    return {"path": labels[int(choice) - 1]}


def _choose_profile(matches: list[BrowserProfile]) -> BrowserProfile | None:
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    for index, item in enumerate(matches, start=1):
        click.echo(
            f"{index}. {item.browser} {item.profile} ({item.cookie_count or 0} cookies)"
        )
    choice = click.prompt(
        "Choose browser profile",
        type=click.Choice([str(i) for i in range(1, len(matches) + 1)]),
        default="1",
        show_choices=False,
    )
    return matches[int(choice) - 1]


def setup_summary(source_id: str, values: dict) -> str:
    kind = provider_kind(source_id)
    if kind == "web":
        auth = values.get("browser") or values.get("browser_path") or "browser"
        return f"{source_id} enabled, auth {auth}, sync every {format_duration_ms(_default_interval(source_id))}"
    return f"{source_id} enabled, path {values.get('path', 'auto')}"


def _default_interval(source_id: str) -> int:
    if source_id == "deepseek":
        return 120_000
    if source_id in {"chatgpt", "claude"}:
        return 60_000
    return 1000
