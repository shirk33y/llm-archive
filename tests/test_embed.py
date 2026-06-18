from __future__ import annotations

import struct

from llm_archive import db
from llm_archive.embed import (
    DEFAULT_MODEL,
    extract_thread_text,
    get_dims,
    serialize,
    threads_needing_embedding,
)


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


def test_extract_thread_text_basic(tmp_path):
    con = db.connect(tmp_path / "test.db")
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
    assert "My Title" in text
    assert "hello there this is long enough" in text


def test_extract_thread_text_skips_short_parts(tmp_path):
    con = db.connect(tmp_path / "test.db")
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
    assert text == "Title"


def test_extract_thread_text_filters_non_text_parts(tmp_path):
    con = db.connect(tmp_path / "test.db")
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
    assert text == "Title"


def test_extract_thread_text_filters_non_user_assistant(tmp_path):
    con = db.connect(tmp_path / "test.db")
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
    assert text == "Title"


def test_extract_thread_text_missing_thread(tmp_path):
    con = db.connect(tmp_path / "test.db")
    db.init_embeddings(con)

    text = extract_thread_text(con, "nonexistent")
    assert text == ""


def test_extract_thread_text_max_chars(tmp_path):
    con = db.connect(tmp_path / "test.db")
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


def test_threads_needing_embedding_all_unembedded(tmp_path):
    con = db.connect(tmp_path / "test.db")
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


def test_threads_needing_embedding_skips_embedded(tmp_path):
    con = db.connect(tmp_path / "test.db")
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


def test_threads_needing_embedding_filter_source(tmp_path):
    con = db.connect(tmp_path / "test.db")
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


def test_threads_needing_embedding_force(tmp_path):
    con = db.connect(tmp_path / "test.db")
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


def test_threads_needing_embedding_force_with_source(tmp_path):
    con = db.connect(tmp_path / "test.db")
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