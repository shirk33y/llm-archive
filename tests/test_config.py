from __future__ import annotations

import pytest

from llm_archive.config import load_config


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
    assert config.ingestor("claude").mode is None


def test_load_config_rejects_invalid_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[ingestors.chatgpt]\nmode = "token"\n')

    with pytest.raises(ValueError, match="Invalid auth mode"):
        load_config(path)
