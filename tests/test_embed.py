from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

import pytest

from llm_archive import db
from llm_archive.embed import (
    embed_text,
    extract_thread_text,
    get_dims,
    serialize,
    threads_needing_embedding,
)


def test_get_dims_known():
    assert get_dims("nomic-embed-text") == 768
    assert get_dims("nomic-embed-text-v1.5") == 768
    assert get_dims("mxbai-embed-large") == 1024
    assert get_dims("all-minilm") == 384


def test_get_dims_unknown_falls_back():
    assert get_dims("unknown-model") == 768
    assert get_dims("") == 768


def test_serialize_roundtrip():
    vector = [1.0, 2.5, -3.0, 0.0, 1.5]
    packed = serialize(vector)
    unpacked = list(struct.unpack(f"{len(vector)}f", packed))
    assert unpacked == vector


def test_serialize_empty():
    assert serialize([]) == b""


@patch("llm_archive.embed.httpx.post")
def test_embed_text(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"embedding": [0.1, 0.2, 0.3]}
    mock_post.return_value = mock_resp

    result = embed_text("hello world")
    assert result == [0.1, 0.2, 0.3]
    mock_post.assert_called_once_with(
        "http://localhost:11434/api/embeddings",
        json={"model": "nomic-embed-text", "prompt": "hello world"},
        timeout=60,
    )


@patch("llm_archive.embed.httpx.post")
def test_embed_text_custom_model_url(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"embedding": [0.5]}
    mock_post.return_value = mock_resp

    result = embed_text("test", model="all-minilm", ollama_url="http://custom:11434")
    assert result == [0.5]
    mock_post.assert_called_once_with(
        "http://custom:11434/api/embeddings",
        json={"model": "all-minilm", "prompt": "test"},
        timeout=60,
    )


@patch("llm_archive.embed.httpx.post")
def test_embed_text_raises_on_http_error(mock_post):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("HTTP 500")
    mock_post.return_value = mock_resp

    with pytest.raises(Exception, match="HTTP 500"):
        embed_text("fail")


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
