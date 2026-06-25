import sqlite3
from collections.abc import Iterator

import pytest

from llm_archive import db


@pytest.fixture(autouse=True)
def isolate_archive_paths(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    db_path = tmp_path / "archive.db"
    monkeypatch.setenv("LLM_ARCHIVE_CONFIG", str(config_path))
    monkeypatch.setenv("LLM_ARCHIVE_DB", str(db_path))
    monkeypatch.delenv("LLM_ARCHIVE_ENABLE_TEST_SOURCES", raising=False)
    monkeypatch.setattr(db, "DB_PATH", db_path)


@pytest.fixture(autouse=True)
def close_sqlite_connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def tracked_connect(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        connections.append(con)
        return con

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    yield
    for con in connections:
        try:
            con.close()
        except sqlite3.ProgrammingError:
            pass
