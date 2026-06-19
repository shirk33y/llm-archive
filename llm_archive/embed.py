from __future__ import annotations

import logging
import struct
from typing import Optional

import litellm

logger = logging.getLogger("llm_archive.embed")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

_FASTEMBED_DIMS: dict[str, int] = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-small-en-v1.5-Q": 384,
}

_cache: dict = {}


def get_dims(model: str, provider: str = "fastembed") -> int:
    if provider == "fastembed":
        return _FASTEMBED_DIMS.get(model, 384)
    try:
        info = litellm.get_model_info(model)
        return info.get("max_input_tokens", 384)
    except Exception:
        return 384


def _get_embedder(model: str = DEFAULT_MODEL):
    key = ("fastembed", model)
    if key not in _cache:
        from fastembed import TextEmbedding

        _cache[key] = TextEmbedding(model)
    return _cache[key]


def embed_text(text: str, model: str = DEFAULT_MODEL, provider: str = "fastembed") -> list[float]:
    if provider == "litellm":
        resp = litellm.embedding(model=model, input=[text])
        return resp["data"][0]["embedding"]
    embedder = _get_embedder(model)
    results = list(embedder.embed([text]))
    return results[0].tolist()


def embed_batch(texts: list[str], model: str = DEFAULT_MODEL, provider: str = "fastembed") -> list[list[float]]:
    if not texts:
        return []
    if provider == "litellm":
        resp = litellm.embedding(model=model, input=texts)
        return [d["embedding"] for d in resp["data"]]
    embedder = _get_embedder(model)
    results = list(embedder.embed(texts))
    return [r.tolist() for r in results]


def serialize(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def extract_thread_text(con, thread_id: str, max_chars: int = 2000) -> str:
    title_row = con.execute("SELECT title FROM threads WHERE id=?", (thread_id,)).fetchone()
    title = (title_row["title"] or "") if title_row else ""

    rows = con.execute(
        """
        SELECT m.role, p.text
        FROM messages m
        JOIN message_parts p ON p.message_id = m.id
        WHERE m.thread_id = ?
          AND p.kind = 'text'
          AND length(p.text) > 10
          AND m.role IN ('user', 'assistant')
        ORDER BY m.created_at, p.ord
        LIMIT 20
        """,
        (thread_id,),
    ).fetchall()

    parts = []
    if title:
        parts.append(f"title: {title}")
    for row in rows:
        text = (row["text"] or "").strip()
        if text:
            role = row["role"]
            parts.append(f"{role}: {text[:500]}")

    return "\n\n".join(parts)[:max_chars]


def threads_needing_embedding(
    con,
    source_id: Optional[str] = None,
    force: bool = False,
) -> list[str]:
    if force:
        q = "SELECT id FROM threads" + (" WHERE source_id=?" if source_id else "")
        rows = con.execute(q, (source_id,) if source_id else ()).fetchall()
    elif source_id:
        rows = con.execute(
            """
            SELECT t.id FROM threads t
            LEFT JOIN thread_embeddings te ON te.thread_id = t.id
            WHERE te.thread_id IS NULL AND t.source_id = ?
            """,
            (source_id,),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT t.id FROM threads t
            LEFT JOIN thread_embeddings te ON te.thread_id = t.id
            WHERE te.thread_id IS NULL
            """
        ).fetchall()
    return [r["id"] for r in rows]


def auto_embed(con, source_id: Optional[str] = None, model: str = DEFAULT_MODEL, provider: str = "fastembed") -> int:
    """Embed threads that don't have embeddings yet.

    Returns the number of threads embedded. Returns 0 if sqlite-vec is
    unavailable, no threads need embedding, or the first embed call fails
    (meaning the user hasn't bootstrapped embeddings yet).

    Used by the service and MCP server to auto-embed after sync.
    """
    import time as _time
    from llm_archive import db

    dims = get_dims(model, provider)
    has_vec, dim_mismatch = db.init_embeddings(con, dims)
    if not has_vec or dim_mismatch:
        return 0

    if db.has_embeddings(con):
        thread_ids = threads_needing_embedding(con, source_id)
    else:
        return 0

    if not thread_ids:
        return 0

    now = int(_time.time() * 1000)
    BATCH_SIZE = 256
    done = 0
    for i in range(0, len(thread_ids), BATCH_SIZE):
        batch_ids = thread_ids[i : i + BATCH_SIZE]
        texts = []
        valid_ids = []
        for tid in batch_ids:
            text = extract_thread_text(con, tid)
            if text.strip():
                texts.append(text)
                valid_ids.append(tid)

        if not texts:
            continue

        try:
            vectors = embed_batch(texts, model, provider)
        except Exception:
            logger.exception("auto_embed: batch failed, stopping")
            break

        for tid, vector in zip(valid_ids, vectors):
            blob = serialize(vector)
            db.upsert_thread_embedding(con, tid, model, blob, now)
        done += len(valid_ids)

    return done