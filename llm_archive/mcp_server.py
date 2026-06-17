"""MCP server for llm-archive — search and retrieve conversations."""
from __future__ import annotations

import asyncio
from typing import Optional

from mcp.server import FastMCP
from llm_archive import db
from llm_archive.config import load_config
from llm_archive.ingestors import INGESTORS
from llm_archive.jobs import ensure_fresh

mcp = FastMCP("llm-archive", json_response=True)


@mcp.tool()
def search_conversations(phrase: str, limit: Optional[int] = None) -> dict:
    """Search message content (FTS5) across all sources.

    Args:
        phrase: Full-text search query
        limit: Max messages to return (default: unlimited, use -n 50 to limit)

    Returns:
        Results dict with 'results' array and count.
    """
    _ensure_fresh()
    con = db.connect()
    try:
        results = db.search_messages(con, phrase, limit)
        return {
            "results": results,
            "count": len(results),
            "query": phrase,
        }
    finally:
        con.close()


@mcp.tool()
def search_threads(phrase: str, limit: Optional[int] = None) -> dict:
    """Find conversations containing search term (groups by thread).

    Args:
        phrase: Search query
        limit: Max results (default: unlimited, use -n 50 to limit)

    Returns:
        Results dict with thread matches grouped by ID.
    """
    _ensure_fresh()
    con = db.connect()
    try:
        results = db.search_threads(con, phrase, limit)
        return {
            "results": results,
            "count": len(results),
            "query": phrase,
        }
    finally:
        con.close()


@mcp.tool()
def list_conversations(limit: Optional[int] = None) -> dict:
    """List all conversations sorted by recency.

    Args:
        limit: Max results (default: unlimited, use -n 50 to limit)

    Returns:
        Results dict with all threads, sorted newest first.
    """
    _ensure_fresh()
    con = db.connect()
    try:
        results = db.list_threads(con, limit)
        return {
            "results": results,
            "count": len(results),
        }
    finally:
        con.close()


@mcp.tool()
def semantic_search(
    query: str,
    limit: Optional[int] = None,
    source_id: Optional[str] = None,
) -> dict:
    """Search conversations by semantic meaning — finds synonyms, related topics, paraphrases.
    Unlike keyword search, finds 'CPU' when searching 'processor', 'login' when searching 'auth'.
    Requires embeddings: run 'llm-archive embed' first (needs ollama + nomic-embed-text).

    Args:
        query: Natural language search query
        limit: Max results (default: unlimited)
        source_id: Filter by source (claudecode, opencode, claude, deepseek, chatgpt)

    Returns:
        Results with threads sorted by relevance. distance=0 is perfect, >0.6 is unrelated.
    """
    from llm_archive import embed as embed_mod

    _ensure_fresh([source_id] if source_id else None)
    con = db.connect()
    try:
        has_vec = db.init_embeddings(con)
        if not has_vec:
            return {
                "error": "sqlite-vec not installed. Run: pip install sqlite-vec",
                "results": [],
                "count": 0,
            }
        try:
            vector = embed_mod.embed_text(query)
        except Exception as e:
            return {
                "error": f"Embedding failed — is ollama running with nomic-embed-text? {e}",
                "results": [],
                "count": 0,
            }
        blob = embed_mod.serialize(vector)
        results = db.semantic_search_threads(con, blob, limit, source_id)
        return {"results": results, "count": len(results), "query": query}
    finally:
        con.close()


@mcp.tool()
def get_conversation(thread_id: str) -> dict:
    """Get full conversation content with all messages.

    Args:
        thread_id: Conversation ID (from search/list results)

    Returns:
        Thread dict with metadata and messages array.
    """
    con = db.connect()
    try:
        thread = db.get_thread(con, thread_id)
        return thread if thread else {"error": f"Thread '{thread_id}' not found"}
    finally:
        con.close()


@mcp.tool()
def get_message(message_id: str) -> dict:
    """Get specific message with parent thread context.

    Args:
        message_id: Message ID (from search results)

    Returns:
        Message dict with content and parent thread info.
    """
    con = db.connect()
    try:
        msg = db.get_message(con, message_id)
        return msg if msg else {"error": f"Message '{message_id}' not found"}
    finally:
        con.close()


@mcp.tool()
def list_sources() -> dict:
    """List all configured sources and sync status.

    Returns:
        Sources dict with all configured sources.
    """
    con = db.connect()
    try:
        sources = con.execute(
            "SELECT id, last_sync, hostname, config FROM sources ORDER BY id"
        ).fetchall()
        return {
            "results": [dict(s) for s in sources],
            "count": len(sources),
        }
    finally:
        con.close()


def run_sync():
    """Entry point for CLI."""
    mcp.run(transport="stdio")


def _ensure_fresh(source_ids: list[str] | None = None) -> None:
    from llm_archive.sync import _sync_one

    async def runner(src: str, force: bool) -> bool:
        return await _sync_one(src, None, None, force, None)

    asyncio.run(
        ensure_fresh(
            source_ids or list(INGESTORS),
            config=load_config(),
            runner=runner,
        )
    )


if __name__ == "__main__":
    run_sync()
