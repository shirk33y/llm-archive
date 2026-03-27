from __future__ import annotations
import sqlite3
import tempfile
from pathlib import Path

import pytest

from llm_archive import db
from llm_archive.schema import IngestedMessage, IngestedThread


def make_thread(thread_id: str, contents: list[str]) -> IngestedThread:
    messages = [
        IngestedMessage(
            id=f"{thread_id}:msg{i}",
            thread_id=f"test:{thread_id}",
            role="user" if i % 2 == 0 else "assistant",
            content=c,
            created_at=i * 1000,
        )
        for i, c in enumerate(contents)
    ]
    return IngestedThread(
        id=f"test:{thread_id}",
        source_id="test",
        title="Test thread",
        created_at=0,
        updated_at=len(contents) * 1000,
        messages=messages,
    )


@pytest.fixture
def con(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_first_write_saves(con):
    thread = make_thread("t1", ["hello", "world"])
    saved = db.save_thread(con, thread)
    assert saved is True

    row = con.execute("SELECT COUNT(*) FROM messages WHERE thread_id='test:t1'").fetchone()
    assert row[0] == 2


def test_identical_content_skipped(con):
    thread = make_thread("t1", ["hello", "world"])
    db.save_thread(con, thread)

    saved = db.save_thread(con, thread)
    assert saved is False


def test_changed_content_updates(con):
    thread_v1 = make_thread("t1", ["hello", "world"])
    db.save_thread(con, thread_v1)

    thread_v2 = make_thread("t1", ["hello", "world", "new message"])
    saved = db.save_thread(con, thread_v2)
    assert saved is True

    row = con.execute("SELECT COUNT(*) FROM messages WHERE thread_id='test:t1'").fetchone()
    assert row[0] == 3


def test_sha1_changes_on_content_edit(con):
    from llm_archive.db import _thread_sha1
    t1 = make_thread("t1", ["hello"])
    t2 = make_thread("t1", ["hello!"])
    assert _thread_sha1(t1) != _thread_sha1(t2)


def test_different_threads_independent(con):
    t1 = make_thread("t1", ["hello"])
    t2 = make_thread("t2", ["hello"])  # same content, different id

    db.save_thread(con, t1)
    saved = db.save_thread(con, t2)
    assert saved is True  # different thread_id → different SHA1
