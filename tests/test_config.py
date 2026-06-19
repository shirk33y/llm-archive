from __future__ import annotations

import pytest

from llm_archive.config import (
    ensure_config,
    format_duration_ms,
    load_config,
    parse_duration_ms,
    update_ingestor_config,
)


def test_load_config_reads_browser_and_ingestor_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '\n'.join(
            [
                'browser_dir = "/tmp/waterfox"',
                "",
                "[ingestors.chatgpt]",
                'mode = "cookies"',
            ]
        )
    )

    config = load_config(path)

    assert config.browser_dir == "/tmp/waterfox"
    assert config.ingestor("chatgpt").mode == "cookies"
    assert config.ingestor("claude").mode == "cookies"
    assert config.ingestor("claude").enabled is False


def test_load_config_rejects_invalid_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[ingestors.chatgpt]\nmode = "token"\n')

    with pytest.raises(Exception, match="Invalid auth mode"):
        load_config(path)


def test_duration_parse_and_format():
    assert parse_duration_ms("100ms") == 100
    assert parse_duration_ms("1.5s") == 1500
    assert parse_duration_ms("23h") == 82_800_000
    assert format_duration_ms(60_000) == "1m"
    assert format_duration_ms(1500) == "1s"


def test_update_ingestor_config_writes_toml(tmp_path):
    path = tmp_path / "config.toml"

    update_ingestor_config(
        "claude",
        {"enabled": True, "mode": "cookies", "sync_interval": "1m"},
        path,
    )

    config = load_config(path)
    assert config.ingestor("claude").enabled is True
    assert config.ingestor("claude").mode == "cookies"
    assert config.ingestor("claude").sync_interval_ms == 60_000


def test_ensure_config_creates_default_disabled_providers(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    browser_root = tmp_path / "firefox"
    profile = browser_root / "default"
    profile.mkdir(parents=True)
    (profile / "cookies.sqlite").touch()
    monkeypatch.setattr("llm_archive.config._detect_browser_dir", lambda: browser_root)

    ensure_config(path)

    text = path.read_text()
    config = load_config(path)
    assert 'browser_dir = "' in text
    assert "[ingestors.chatgpt]" in text
    assert "[ingestors.claudecode]" in text
    assert config.ingestor("chatgpt").enabled is False
    assert config.ingestor("chatgpt").sync_interval_ms == 1_800_000
    assert config.ingestor("claudecode").watch is True


def test_load_config_embed_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[ingestors.chatgpt]\nmode = \"cookies\"\n")

    config = load_config(path)
    assert config.embed.auto is True
    assert config.embed.provider == "fastembed"
    assert config.embed.model == "BAAI/bge-small-en-v1.5"


def test_load_config_embed_auto_enabled(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[embed]\nauto = true\n\n[ingestors.chatgpt]\nmode = \"cookies\"\n")

    config = load_config(path)
    assert config.embed.auto is True


def test_load_config_embed_auto_disabled(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[embed]\nauto = false\n\n[ingestors.chatgpt]\nmode = \"cookies\"\n")

    config = load_config(path)
    assert config.embed.auto is False


def test_ensure_config_includes_embed_and_summarize(tmp_path, monkeypatch):
    browser_root = tmp_path / "firefox"
    profile = browser_root / "default"
    profile.mkdir(parents=True)
    (profile / "cookies.sqlite").touch()
    monkeypatch.setattr("llm_archive.config._detect_browser_dir", lambda: browser_root)
    path = tmp_path / "config.toml"
    ensure_config(path)
    text = path.read_text()
    assert "[embed]" in text
    assert "auto = true" in text
    assert "[summarize]" in text

    config = load_config(path)
    assert config.embed.auto is True
    assert config.summarize.auto is False
    assert config.summarize.min_new_messages == 3


def test_load_config_summarize_custom(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[summarize]\nauto = true\nmodel = \"anthropic/claude-sonnet-4-20250514\"\nmin_new_messages = 5\n"
    )

    config = load_config(path)
    assert config.summarize.auto is True
    assert config.summarize.model == "anthropic/claude-sonnet-4-20250514"
    assert config.summarize.min_new_messages == 5
