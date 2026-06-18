from __future__ import annotations

from unittest.mock import patch

import pytest

import click

from llm_archive.setup import _default_interval, _enable_file, setup_summary


class TestDefaultInterval:
    def test_deepseek(self):
        assert _default_interval("deepseek") == 120_000

    def test_chatgpt(self):
        assert _default_interval("chatgpt") == 60_000

    def test_claude(self):
        assert _default_interval("claude") == 60_000

    def test_other(self):
        assert _default_interval("claudecode") == 1000

    def test_custom(self):
        assert _default_interval("codex") == 1000


class TestEnableFile:
    def test_with_explicit_path(self, tmp_path):
        target = tmp_path / "data.db"
        target.touch()
        result = _enable_file("claudecode", str(target))
        assert result == {"path": str(target)}

    def test_no_path_no_data_raises(self):
        with patch("llm_archive.setup.provider_paths", return_value=[]):
            with pytest.raises(click.ClickException, match="No data path found"):
                _enable_file("claudecode", None)

    def test_single_existing_path(self, tmp_path):
        data = tmp_path / "conversations" / "claudecode"
        data.mkdir(parents=True)
        with patch("llm_archive.setup.provider_paths", return_value=[data]):
            result = _enable_file("claudecode", None)
            assert result == {"path": str(data)}

    def test_single_nonexistent_default(self, tmp_path):
        data = tmp_path / "does-not-exist"
        with patch("llm_archive.setup.provider_paths", return_value=[data]):
            result = _enable_file("claudecode", None)
            assert result == {"path": str(data)}

    def test_explicit_path_nonexistent(self, tmp_path):
        target = tmp_path / "nonexistent.db"
        result = _enable_file("claudecode", str(target))
        assert result == {"path": str(target)}


class TestSetupSummary:
    def test_web_provider(self):
        values = {"browser": "chrome", "enabled": True}
        result = setup_summary("deepseek", values)
        assert "deepseek" in result
        assert "chrome" in result

    def test_file_provider(self):
        values = {"path": "/some/path", "enabled": True}
        result = setup_summary("claudecode", values)
        assert "claudecode" in result
        assert "/some/path" in result

    def test_web_with_browser_path(self):
        values = {"browser_path": "/usr/bin/chrome", "enabled": True}
        result = setup_summary("chatgpt", values)
        assert "chatgpt" in result
        assert "/usr/bin/chrome" in result