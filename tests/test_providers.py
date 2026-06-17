from __future__ import annotations

from pathlib import Path

from llm_archive.providers import provider_kind, provider_paths


def test_provider_kind_web():
    assert provider_kind("chatgpt") == "web"
    assert provider_kind("claude") == "web"
    assert provider_kind("deepseek") == "web"


def test_provider_kind_file():
    assert provider_kind("claudecode") == "file"
    assert provider_kind("codex") == "file"
    assert provider_kind("cursor") == "file"
    assert provider_kind("gemini") == "file"
    assert provider_kind("opencode") == "file"


def test_provider_kind_service():
    assert provider_kind("windsurf") == "service"


def test_provider_kind_unknown_defaults_to_file():
    assert provider_kind("nonexistent") == "file"
    assert provider_kind("") == "file"


def test_provider_paths_file_providers():
    codex_paths = provider_paths("codex")
    assert len(codex_paths) == 2
    assert all(isinstance(p, Path) for p in codex_paths)

    claudecode_paths = provider_paths("claudecode")
    assert len(claudecode_paths) == 1


def test_provider_paths_web_providers():
    assert provider_paths("chatgpt") == ()
    assert provider_paths("claude") == ()
    assert provider_paths("deepseek") == ()


def test_provider_paths_unknown():
    paths = provider_paths("nonexistent")
    assert paths == ()
