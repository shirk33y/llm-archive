from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProviderInfo:
    source_id: str
    kind: str
    paths: tuple[Path, ...] = ()
    domains: tuple[str, ...] = ()


PROVIDERS: dict[str, ProviderInfo] = {
    "chatgpt": ProviderInfo("chatgpt", "web", domains=("chatgpt.com", "openai.com")),
    "claude": ProviderInfo("claude", "web", domains=("claude.ai",)),
    "deepseek": ProviderInfo("deepseek", "web", domains=("chat.deepseek.com", "deepseek.com")),
    "claudecode": ProviderInfo("claudecode", "file", paths=(Path.home() / ".claude" / "projects",)),
    "codex": ProviderInfo(
        "codex",
        "file",
        paths=(Path.home() / ".codex" / "state_5.sqlite", Path.home() / ".codex" / "sessions"),
    ),
    "opencode": ProviderInfo(
        "opencode",
        "file",
        paths=(Path.home() / ".local" / "share" / "opencode" / "opencode.db",),
    ),
    "windsurf": ProviderInfo("windsurf", "service"),
}


def provider_kind(source_id: str) -> str:
    return PROVIDERS.get(source_id, ProviderInfo(source_id, "file")).kind


def provider_paths(source_id: str) -> tuple[Path, ...]:
    return PROVIDERS.get(source_id, ProviderInfo(source_id, "file")).paths
