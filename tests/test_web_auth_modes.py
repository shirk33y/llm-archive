from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_claude_cookie_mode_uses_browser_cookies(monkeypatch):
    from llm_archive.ingestors import claude

    seen = {}

    def fake_extract_firefox_cookies(*, browser_dir=None, domains=None):
        seen["browser_dir"] = browser_dir
        seen["domains"] = domains
        return [{"name": "sessionKey", "value": "cookie", "domain": ".claude.ai", "path": "/"}]

    monkeypatch.setattr(claude, "extract_firefox_cookies", fake_extract_firefox_cookies)

    ingestor = claude.ClaudeIngestor(auth_mode="cookies", browser_dir="/browser")

    assert await ingestor._get_cookies() == {"sessionKey": "cookie"}
    assert seen == {"browser_dir": "/browser", "domains": ("claude.ai",)}


def test_deepseek_cookie_mode_reads_token_from_browser_storage(monkeypatch):
    from llm_archive.ingestors import deepseek

    seen = {}

    def fake_extract_firefox_local_storage(origin, *, browser_dir=None):
        seen["origin"] = origin
        seen["browser_dir"] = browser_dir
        return {"userToken": '{"value":"tok","__version":"0"}'}

    monkeypatch.setattr(
        deepseek,
        "extract_firefox_local_storage",
        fake_extract_firefox_local_storage,
    )

    ingestor = deepseek.DeepseekIngestor(auth_mode="cookies", browser_dir="/browser")

    assert ingestor._get_token_from_browser_storage() == "tok"
    assert seen == {"origin": "https://chat.deepseek.com", "browser_dir": "/browser"}


@pytest.mark.asyncio
async def test_deepseek_cookie_mode_uses_browser_cookies(monkeypatch):
    from llm_archive.ingestors import deepseek

    seen = {}

    def fake_extract_firefox_cookies(*, browser_dir=None, domains=None):
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

    monkeypatch.setattr(deepseek, "extract_firefox_cookies", fake_extract_firefox_cookies)

    ingestor = deepseek.DeepseekIngestor(auth_mode="cookies", browser_dir="/browser")

    assert await ingestor._get_cookies() == {"ds_session_id": "cookie"}
    assert seen == {
        "browser_dir": "/browser",
        "domains": ("chat.deepseek.com", "deepseek.com"),
    }
