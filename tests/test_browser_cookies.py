from __future__ import annotations

import sqlite3
import time

from llm_archive.auth.browser_cookies import (
    cookie_header_for_url,
    extract_browser_cookies,
    extract_browser_local_storage_value,
    extract_firefox_cookies,
    extract_firefox_local_storage,
)


def test_extract_firefox_cookies_filters_domains_and_builds_header(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    db_path = profile / "cookies.sqlite"

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version=16")
    conn.execute(
        "CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)"
    )
    expires = int((time.time() + 3600) * 1000)
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?)",
        (".chatgpt.com", "session", "abc", "/", expires, 1),
    )
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?)",
        (".example.com", "skip", "nope", "/", expires, 1),
    )
    conn.commit()
    conn.close()

    cookies = extract_firefox_cookies(
        browser_dir=str(tmp_path),
        domains=("chatgpt.com",),
    )
    header = cookie_header_for_url(cookies, "https://chatgpt.com/api/auth/session")

    assert [cookie["name"] for cookie in cookies] == ["session"]
    assert header == "session=abc"


def test_extract_firefox_local_storage(tmp_path):
    profile = tmp_path / "profile"
    storage = profile / "storage/default/https+++chat.deepseek.com/ls"
    storage.mkdir(parents=True)
    (profile / "cookies.sqlite").touch()
    db_path = storage / "data.sqlite"

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE data (key TEXT, value BLOB)")
    conn.execute(
        "INSERT INTO data VALUES (?, ?)",
        ("userToken", b'{"value":"tok","__version":"0"}'),
    )
    conn.commit()
    conn.close()

    values = extract_firefox_local_storage(
        "https://chat.deepseek.com",
        browser_dir=str(tmp_path),
    )

    assert values == {"userToken": '{"value":"tok","__version":"0"}'}


def test_extract_browser_cookies_uses_firefox_adapter(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    db_path = profile / "cookies.sqlite"

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version=16")
    conn.execute(
        "CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)"
    )
    expires = int((time.time() + 3600) * 1000)
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?)",
        (".claude.ai", "session", "abc", "/", expires, 1),
    )
    conn.commit()
    conn.close()

    cookies = extract_browser_cookies(
        "waterfox",
        browser_dir=str(profile),
        domains=("claude.ai",),
    )

    assert cookies[0]["name"] == "session"


def test_extract_browser_local_storage_value_reads_chromium_leveldb(tmp_path):
    storage = tmp_path / "Local Storage" / "leveldb"
    storage.mkdir(parents=True)
    (storage / "000003.log").write_bytes(
        b'noise userToken {"value":"tok","__version":"0"}'
    )

    value = extract_browser_local_storage_value(
        "https://chat.deepseek.com",
        "userToken",
        browser="chrome",
        browser_dir=str(tmp_path),
    )

    assert value == '{"value": "tok"}'


def test_firefox_browser_dir_with_profile_does_not_double(tmp_path):
    """browser_dir pointing at a profile dir should not get profile name appended again."""
    from llm_archive.auth.browser_cookies import _firefox_search_roots

    profile_dir = tmp_path / "c4ltxd61.default-release"
    profile_dir.mkdir()
    roots = _firefox_search_roots(str(profile_dir), "c4ltxd61.default-release")
    assert roots == [str(profile_dir)]
    assert "c4ltxd61.default-release/c4ltxd61.default-release" not in roots[0]


def test_firefox_search_roots_profile_only():
    """Without browser_dir, profile should be appended to default roots."""
    from llm_archive.auth.browser_cookies import _firefox_search_roots

    roots = _firefox_search_roots(None, "abc.default")
    assert all("abc.default" in r for r in roots)
