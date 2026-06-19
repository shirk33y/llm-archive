from __future__ import annotations

import struct

import pytest

from llm_archive import db
from llm_archive.embed import (
    DEFAULT_MODEL,
    auto_embed,
    embed_batch,
    extract_thread_text,
    get_dims,
    serialize,
    threads_needing_embedding,
)
from llm_archive.schema import IngestedMessage, IngestedThread


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def test_get_dims_default():
    assert get_dims(DEFAULT_MODEL) == 384


def test_get_dims_known():
    assert get_dims("BAAI/bge-small-en-v1.5") == 384
    assert get_dims("BAAI/bge-small-en-v1.5-Q") == 384


def test_get_dims_unknown_falls_back():
    assert get_dims("unknown-model") == 384


def test_serialize_roundtrip():
    vector = [1.0, 2.5, -3.0, 0.0, 1.5]
    packed = serialize(vector)
    unpacked = list(struct.unpack(f"{len(vector)}f", packed))
    assert unpacked == vector


def test_serialize_empty():
    assert serialize([]) == b""


def test_embed_text_caches_model():
    from llm_archive.embed import _cache, embed_text

    _cache.clear()
    try:
        result = embed_text("test query")
        assert len(result) == 384
        key = ("fastembed", DEFAULT_MODEL)
        assert key in _cache, "model should be cached after first call"
        first_model = _cache[key]
        embed_text("second query")
        assert _cache[key] is first_model, "should reuse cached model"
    finally:
        _cache.clear()


def test_extract_thread_text_basic(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('test')")
    con.execute(
        "INSERT INTO threads(id, source_id, title, created_at, updated_at) "
        "VALUES ('test:t1', 'test', 'My Title', 1000, 1000)"
    )
    con.execute(
        "INSERT INTO messages(id, thread_id, role, content, created_at) "
        "VALUES ('test:m1', 'test:t1', 'user', 'hello there', 1000)"
    )
    con.execute(
        "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
        "VALUES ('test:m1', 0, 'text', 'hello there this is long enough', 1, 1)"
    )
    con.commit()

    text = extract_thread_text(con, "test:t1")
    assert "title: My Title" in text
    assert "user: hello there this is long enough" in text


def test_extract_thread_text_skips_short_parts(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('test')")
    con.execute(
        "INSERT INTO threads(id, source_id, title, created_at, updated_at) "
        "VALUES ('test:t1', 'test', 'Title', 1000, 1000)"
    )
    con.execute(
        "INSERT INTO messages(id, thread_id, role, content, created_at) "
        "VALUES ('test:m1', 'test:t1', 'user', 'short', 1000)"
    )
    con.execute(
        "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
        "VALUES ('test:m1', 0, 'text', 'short', 1, 1)"
    )
    con.commit()

    text = extract_thread_text(con, "test:t1")
    assert text == "title: Title"


def test_extract_thread_text_filters_non_text_parts(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('test')")
    con.execute(
        "INSERT INTO threads(id, source_id, title, created_at, updated_at) "
        "VALUES ('test:t1', 'test', 'Title', 1000, 1000)"
    )
    con.execute(
        "INSERT INTO messages(id, thread_id, role, content, created_at) "
        "VALUES ('test:m1', 'test:t1', 'assistant', 'tool result', 1000)"
    )
    con.execute(
        "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
        "VALUES ('test:m1', 0, 'tool_result', 'some tool output that is long enough', 1, 0)"
    )
    con.commit()

    text = extract_thread_text(con, "test:t1")
    assert text == "title: Title"


def test_extract_thread_text_filters_non_user_assistant(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('test')")
    con.execute(
        "INSERT INTO threads(id, source_id, title, created_at, updated_at) "
        "VALUES ('test:t1', 'test', 'Title', 1000, 1000)"
    )
    con.execute(
        "INSERT INTO messages(id, thread_id, role, content, created_at) "
        "VALUES ('test:m1', 'test:t1', 'system', 'system prompt that is long enough', 1000)"
    )
    con.execute(
        "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
        "VALUES ('test:m1', 0, 'text', 'system prompt that is long enough', 1, 1)"
    )
    con.commit()

    text = extract_thread_text(con, "test:t1")
    assert text == "title: Title"


def test_extract_thread_text_missing_thread(con):
    db.init_embeddings(con)

    text = extract_thread_text(con, "nonexistent")
    assert text == ""


def test_extract_thread_text_max_chars(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('test')")
    con.execute(
        "INSERT INTO threads(id, source_id, title, created_at, updated_at) "
        "VALUES ('test:t1', 'test', 'x' * 100, 1000, 1000)"
    )
    con.execute(
        "INSERT INTO messages(id, thread_id, role, content, created_at) "
        "VALUES ('test:m1', 'test:t1', 'user', 'y' * 500, 1000)"
    )
    con.execute(
        "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
        "VALUES ('test:m1', 0, 'text', 'y' * 500, 1, 1)"
    )
    con.commit()

    text = extract_thread_text(con, "test:t1", max_chars=50)
    assert len(text) <= 50


def test_threads_needing_embedding_all_unembedded(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('test')")
    con.execute(
        "INSERT INTO threads(id, source_id, title) VALUES ('test:t1', 'test', 'A')"
    )
    con.execute(
        "INSERT INTO threads(id, source_id, title) VALUES ('test:t2', 'test', 'B')"
    )
    con.commit()

    result = threads_needing_embedding(con)
    assert sorted(result) == ["test:t1", "test:t2"]


def test_threads_needing_embedding_skips_embedded(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('test')")
    con.execute(
        "INSERT INTO threads(id, source_id, title) VALUES ('test:t1', 'test', 'A')"
    )
    con.execute(
        "INSERT INTO threads(id, source_id, title) VALUES ('test:t2', 'test', 'B')"
    )
    con.execute(
        "INSERT INTO thread_embeddings(thread_id, model, embedded_at) VALUES ('test:t1', 'test', 1000)"
    )
    con.commit()

    result = threads_needing_embedding(con)
    assert result == ["test:t2"]


def test_threads_needing_embedding_filter_source(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('a')")
    con.execute("INSERT INTO sources(id) VALUES ('b')")
    con.execute(
        "INSERT INTO threads(id, source_id, title) VALUES ('a:t1', 'a', 'A')"
    )
    con.execute(
        "INSERT INTO threads(id, source_id, title) VALUES ('b:t1', 'b', 'B')"
    )
    con.commit()

    result = threads_needing_embedding(con, source_id="a")
    assert result == ["a:t1"]


def test_threads_needing_embedding_force(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('test')")
    con.execute(
        "INSERT INTO threads(id, source_id, title) VALUES ('test:t1', 'test', 'A')"
    )
    con.execute(
        "INSERT INTO thread_embeddings(thread_id, model, embedded_at) VALUES ('test:t1', 'test', 1000)"
    )
    con.commit()

    result = threads_needing_embedding(con, force=True)
    assert result == ["test:t1"]


def test_threads_needing_embedding_force_with_source(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('a')")
    con.execute("INSERT INTO sources(id) VALUES ('b')")
    con.execute(
        "INSERT INTO threads(id, source_id, title) VALUES ('a:t1', 'a', 'A')"
    )
    con.execute(
        "INSERT INTO threads(id, source_id, title) VALUES ('b:t1', 'b', 'B')"
    )
    con.commit()

    result = threads_needing_embedding(con, source_id="a", force=True)
    assert result == ["a:t1"]


def test_embed_batch_empty():
    assert embed_batch([]) == []


def test_embed_batch_returns_vectors():
    from llm_archive.embed import _cache

    _cache.clear()
    try:
        results = embed_batch(["hello world", "foo bar"])
        assert len(results) == 2
        assert len(results[0]) == 384
        assert len(results[1]) == 384
    finally:
        _cache.clear()


def test_extract_thread_text_includes_role_prefix(con):
    db.init_embeddings(con)

    con.execute("INSERT INTO sources(id) VALUES ('test')")
    con.execute(
        "INSERT INTO threads(id, source_id, title, created_at, updated_at) "
        "VALUES ('test:t1', 'test', 'React Hooks Discussion', 1000, 1000)"
    )
    con.execute(
        "INSERT INTO messages(id, thread_id, role, content, created_at) "
        "VALUES ('test:m1', 'test:t1', 'user', 'how do I use useEffect', 1000)"
    )
    con.execute(
        "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
        "VALUES ('test:m1', 0, 'text', 'how do I use useEffect for data fetching', 1, 1)"
    )
    con.execute(
        "INSERT INTO messages(id, thread_id, role, content, created_at) "
        "VALUES ('test:m2', 'test:t1', 'assistant', 'useEffect runs after render', 2000)"
    )
    con.execute(
        "INSERT INTO message_parts(message_id, ord, kind, text, visible, searchable) "
        "VALUES ('test:m2', 0, 'text', 'useEffect runs after render and can fetch data on mount', 1, 1)"
    )
    con.commit()

    text = extract_thread_text(con, "test:t1")
    assert text.startswith("title: React Hooks Discussion")
    assert "user: how do I use useEffect" in text
    assert "assistant: useEffect runs after render" in text


def test_auto_embed_no_threads(con):
    db.init_embeddings(con)
    result = auto_embed(con)
    assert result == 0


def test_auto_embed_skips_if_no_existing_embeddings(con, monkeypatch):
    db.init_embeddings(con)
    db.save_thread(
        con,
        IngestedThread(
            id="test:t1",
            source_id="test",
            title="Hello world",
            created_at=1000,
            updated_at=2000,
            messages=[
                IngestedMessage(
                    id="test:m1",
                    thread_id="test:t1",
                    role="user",
                    content="how do I use useEffect for data fetching",
                    created_at=1000,
                ),
            ],
        ),
    )
    con.commit()
    result = auto_embed(con)
    assert result == 0


def test_auto_embed_embeds_new_threads(con, monkeypatch):
    has_vec, _ = db.init_embeddings(con, 384)
    if not has_vec:
        pytest.skip("sqlite-vec not available")
    db.save_thread(
        con,
        IngestedThread(
            id="test:t1",
            source_id="test",
            title="Hello world",
            created_at=1000,
            updated_at=2000,
            messages=[
                IngestedMessage(
                    id="test:m1",
                    thread_id="test:t1",
                    role="user",
                    content="how do I use useEffect for data fetching",
                    created_at=1000,
                ),
            ],
        ),
    )
    con.commit()
    monkeypatch.setattr("llm_archive.db.has_embeddings", lambda c: True)
    monkeypatch.setattr(
        "llm_archive.embed.embed_batch",
        lambda texts, model=None, provider="fastembed": [[0.1] * 384 for _ in texts],
    )
    result = auto_embed(con)
    assert result == 1
    row = con.execute("SELECT COUNT(*) FROM thread_embeddings").fetchone()
    assert row[0] == 1