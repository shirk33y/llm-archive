"""Semantic embedding for threads using Ollama."""
from __future__ import annotations

import struct
from typing import Optional

import httpx

OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "nomic-embed-text"

_MODEL_DIMS: dict[str, int] = {
    "nomic-embed-text": 768,
    "nomic-embed-text-v1.5": 768,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
}


def get_dims(model: str) -> int:
    return _MODEL_DIMS.get(model, 768)


def embed_text(
    text: str,
    model: str = DEFAULT_MODEL,
    ollama_url: str = OLLAMA_URL,
) -> list[float]:
    resp = httpx.post(
        f"{ollama_url}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def serialize(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def extract_thread_text(con, thread_id: str, max_chars: int = 2000) -> str:
    """Extract clean, embeddable text from a thread (title + user/assistant messages, no tool noise)."""
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
        parts.append(title)
    for row in rows:
        text = (row["text"] or "").strip()
        if text:
            parts.append(text[:500])

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
