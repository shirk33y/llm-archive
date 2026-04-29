"""MCP server for llm-archive — search and retrieve conversations."""
from __future__ import annotations

import json
from typing import Any

from mcp.server import FastMCP
from llm_archive import db

mcp = FastMCP("llm-archive", json_response=True)


@mcp.tool()
def search_conversations(phrase: str, limit: int = 50) -> dict:
    """Search message content (FTS5) across all sources.

    Args:
        phrase: Full-text search query
        limit: Max messages to return (default: 50)

    Returns:
        Results dict with 'results' array and count.
    """
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
def search_threads(phrase: str, limit: int = 50) -> dict:
    """Find conversations containing search term (groups by thread).

    Args:
        phrase: Search query
        limit: Max results (default: 50)

    Returns:
        Results dict with thread matches grouped by ID.
    """
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
def list_conversations(limit: int = 100) -> dict:
    """List all conversations sorted by recency.

    Args:
        limit: Max results (default: 100)

    Returns:
        Results dict with all threads, sorted newest first.
    """
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


if __name__ == "__main__":
    run_sync()
