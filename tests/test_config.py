from __future__ import annotations

import pytest

from llm_archive.config import (
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

    with pytest.raises(ValueError, match="Invalid auth mode"):
        load_config(path)


def test_duration_parse_and_format():
    assert parse_duration_ms("100ms") == 100
    assert parse_duration_ms("1.5s") == 1500
    assert parse_duration_ms("23h") == 82_800_000
    assert format_duration_ms(60_000) == "1m"
    assert format_duration_ms(1500) == "1500ms"


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
