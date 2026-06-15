from __future__ import annotations

import sqlite3
import time

from llm_archive.auth.browser_cookies import (
    cookie_header_for_url,
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
