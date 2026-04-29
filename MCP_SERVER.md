# llm-archive MCP Server

Model Context Protocol server for searching and retrieving conversations from the llm-archive database. Exposes tools for LLM-based conversation analysis and retrieval.

## Architecture

MCP server wraps existing `llm_archive.db` query functions as tools. Install as a Python dependency and configure via `~/.claude/cline_mcp_settings.json` (Claude) or equivalent for other clients.

```
llm_archive/
├── mcp_server.py        # FastMCP server implementation
├── db.py               # (existing) Query functions
├── schema.py           # (existing) Dataclasses
└── ...
```

## Tools

All tools accept `phrase` (search term) and `limit` (result count, default 50). Results include context: source_id, thread_id, title, timestamps.

### 1. `search_conversations`
Search message content across all sources (FTS5).

**Input:**
- `phrase` (string): Full-text search query
- `limit` (int, optional): Max messages to return (default: 50)

**Output:** Array of matching messages with:
- `thread_id`, `title`, `source_id`
- `message_id`, `role` (user/assistant)
- `content_clean` (text), `created_at` (unix timestamp)
- `kind` (message part type: text, code, image, etc.)

**Use case:** Find specific discussions or code snippets across all archived conversations.

### 2. `search_threads`
Find conversations matching a query (groups results by thread).

**Input:**
- `phrase` (string): Search term
- `limit` (int, optional): Max threads to return (default: 50)

**Output:** Array of threads with:
- `thread_id`, `title`, `source_id`
- `match_count` (number of matching messages in thread)
- `last_match_at` (when last match occurred)

**Use case:** Discover which conversations contain relevant discussion about a topic.

### 3. `list_conversations`
List all conversations sorted by recency.

**Input:**
- `limit` (int, optional): Max threads to return (default: 100)

**Output:** Array of threads with basic metadata (no content).

**Use case:** Browse conversation index without searching.

### 4. `get_conversation`
Retrieve full conversation content with all messages.

**Input:**
- `thread_id` (string): Conversation ID (from search/list results)

**Output:**
- `thread_id`, `title`, `source_id`
- `created_at`, `updated_at`
- `messages` array with:
  - `id`, `role`, `created_at`
  - `parts` array (text, code blocks, etc. with metadata)

**Use case:** Load full context for detailed analysis or continuation.

### 5. `get_message`
Retrieve a single message with parent thread context.

**Input:**
- `message_id` (string): Message ID (from search results)

**Output:**
- `message_id`, `role`, `created_at`
- `parts` (formatted content)
- `thread_id`, `title` (parent context)

**Use case:** View specific message in conversation context.

## Implementation Example

```python
# llm_archive/mcp_server.py
from mcp import FastMCP
from llm_archive import db
from pathlib import Path

mcp = FastMCP("llm-archive", json_response=True)

@mcp.tool()
def search_conversations(phrase: str, limit: int = 50) -> dict:
    """Search message content (FTS5) across all sources."""
    con = db.connect()
    try:
        results = db.search_messages(con, phrase, limit)
        return {"results": results, "count": len(results)}
    finally:
        con.close()

@mcp.tool()
def search_threads(phrase: str, limit: int = 50) -> dict:
    """Find conversations containing search term."""
    con = db.connect()
    try:
        results = db.search_threads(con, phrase, limit)
        return {"results": results, "count": len(results)}
    finally:
        con.close()

@mcp.tool()
def list_conversations(limit: int = 100) -> dict:
    """List all conversations sorted by recency."""
    con = db.connect()
    try:
        results = db.list_threads(con, limit)
        return {"results": results, "count": len(results)}
    finally:
        con.close()

@mcp.tool()
def get_conversation(thread_id: str) -> dict:
    """Get full conversation content."""
    con = db.connect()
    try:
        thread = db.get_thread(con, thread_id)
        if not thread:
            return {"error": f"Thread {thread_id} not found"}
        return thread
    finally:
        con.close()

@mcp.tool()
def get_message(message_id: str) -> dict:
    """Get specific message with context."""
    con = db.connect()
    try:
        msg = db.get_message(con, message_id)
        if not msg:
            return {"error": f"Message {message_id} not found"}
        return msg
    finally:
        con.close()

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

## Installation

1. Add to `pyproject.toml`:
```toml
[project.optional-dependencies]
mcp = ["mcp>=0.6.0"]

[project.scripts]
llm-archive-mcp = "llm_archive.mcp_server:run"
```

2. Install with MCP support:
```sh
uv pip install -e ".[mcp]"
```

3. Configure in Claude Code:
```json
{
  "mcpServers": {
    "llm-archive": {
      "command": "llm-archive-mcp",
      "env": {
        "CLAUDE_HOME": "/home/user/.claude"
      }
    }
  }
}
```

## Design Decisions

**JSON responses**: Tools return plain JSON (no formatted text) so LLMs can process structure directly. Format for display in prompt, not in tool output.

**Connection pooling**: Each tool opens/closes connection separately. For high-volume use, add connection pooling or context manager.

**FTS5 backend**: Leverages existing message_parts_fts table — client doesn't need to know about indexing.

**Deduplication via SHA1**: Already handled by db layer; MCP server just reads deduplicated data.

**Error handling**: Returns `{"error": "message"}` on failures; client can detect and handle gracefully.

## Future Extensions

- `sync_source(source_id)` — Trigger sync for specific source
- `export_conversation(thread_id, format)` — Export as markdown/JSON
- `summarize_conversation(thread_id)` — Generate summary (requires LLM call)
- `list_sources()` — Show configured sources and sync status
- Resource-based interface for loading conversations as context (not tools)

## Testing

```sh
# Run MCP server in stdio transport (for manual testing)
uv run llm-archive-mcp

# Send JSON-RPC requests via stdin
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_conversations","arguments":{"phrase":"authentication"}}}' | llm-archive-mcp
```

## References

- [MCP Python SDK](https://modelcontextprotocol.github.io/python-sdk/)
- [Real Python MCP Tutorial](https://realpython.com/python-mcp/)
- [Official MCP Spec](https://spec.modelcontextprotocol.io/)
