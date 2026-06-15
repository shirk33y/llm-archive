from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_claude_cookie_mode_uses_browser_cookies(monkeypatch):
    from llm_archive.ingestors import claude

    seen = {}

    def fake_extract_browser_cookies(browser=None, profile=None, *, browser_dir=None, domains=None):
        seen["browser"] = browser
        seen["profile"] = profile
        seen["browser_dir"] = browser_dir
        seen["domains"] = domains
        return [{"name": "sessionKey", "value": "cookie", "domain": ".claude.ai", "path": "/"}]

    monkeypatch.setattr(claude, "extract_browser_cookies", fake_extract_browser_cookies)

    ingestor = claude.ClaudeIngestor(auth_mode="cookies", browser_dir="/browser")

    assert await ingestor._get_cookies() == {"sessionKey": "cookie"}
    assert seen == {
        "browser": None,
        "profile": None,
        "browser_dir": "/browser",
        "domains": ("claude.ai",),
    }


def test_deepseek_cookie_mode_reads_token_from_browser_storage(monkeypatch):
    from llm_archive.ingestors import deepseek

    seen = {}

    def fake_extract_browser_local_storage_value(
        origin,
        key,
        browser=None,
        profile=None,
        *,
        browser_dir=None,
    ):
        seen["origin"] = origin
        seen["key"] = key
        seen["browser"] = browser
        seen["profile"] = profile
        seen["browser_dir"] = browser_dir
        return '{"value":"tok","__version":"0"}'

    monkeypatch.setattr(
        deepseek,
        "extract_browser_local_storage_value",
        fake_extract_browser_local_storage_value,
    )

    ingestor = deepseek.DeepseekIngestor(auth_mode="cookies", browser_dir="/browser")

    assert ingestor._get_token_from_browser_storage() == "tok"
    assert seen == {
        "origin": "https://chat.deepseek.com",
        "key": "userToken",
        "browser": None,
        "profile": None,
        "browser_dir": "/browser",
    }


@pytest.mark.asyncio
async def test_deepseek_cookie_mode_uses_browser_cookies(monkeypatch):
    from llm_archive.ingestors import deepseek

    seen = {}

    def fake_extract_browser_cookies(browser=None, profile=None, *, browser_dir=None, domains=None):
        seen["browser"] = browser
        seen["profile"] = profile
        seen["browser_dir"] = browser_dir
        seen["domains"] = domains
        return [
            {
                "name": "ds_session_id",
                "value": "cookie",
                "domain": ".chat.deepseek.com",
                "path": "/",
            }
        ]

    monkeypatch.setattr(deepseek, "extract_browser_cookies", fake_extract_browser_cookies)

    ingestor = deepseek.DeepseekIngestor(auth_mode="cookies", browser_dir="/browser")

    assert await ingestor._get_cookies() == {"ds_session_id": "cookie"}
    assert seen == {
        "browser": None,
        "profile": None,
        "browser_dir": "/browser",
        "domains": ("chat.deepseek.com", "deepseek.com"),
    }
