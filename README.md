# llm-archive

A CLI tool that dumps and syncs AI conversations from multiple sources into a local SQLite database.

## Supported sources

| Source | Status | Method |
|--------|--------|--------|
| `claudecode` | ✅ working | local JSONL (`~/.claude/projects/`) |
| `opencode` | ✅ working | local SQLite (`~/.local/share/opencode/opencode.db`) |
| `claude` | ✅ working | claude.ai REST API + Playwright auth |
| `deepseek` | ✅ working | deepseek.com REST API + CDP auth |
| `windsurf` | ✅ working | local Language Server API (auto-detected port) |
| `chatgpt` | ✅ working | chatgpt.com REST API + CDP auth |

**Note on web-based ingestors:** Headless browsers (Playwright/Selenium) trigger Cloudflare CAPTCHA loops. Web-based sources (claude, deepseek, chatgpt) require a Chrome browser with remote debugging enabled and an active session. See [CDP setup](#cdp-setup) below.

## Installation

### Development (local, with venv)
```sh
git clone https://github.com/shirk33y/llm-archive
cd llm-archive
uv venv && uv sync
uv run llm-archive --help
```

### User installation (global binary in ~/.local/bin)
```sh
uv tool install /path/to/llm-archive
```
Binary installed to `~/.local/bin/llm-archive`. Ensure it's in your PATH (usually already configured).

### Global installation (system-wide)
```sh
sudo uv tool install /path/to/llm-archive --global
```

## Usage

### Sync

```sh
uv run llm-archive sync               # sync all sources
uv run llm-archive sync claudecode   # sync one source
uv run llm-archive sync deepseek      # sync deepseek (opens browser for first-time auth)
uv run llm-archive sync windsurf      # sync windsurf (requires Windsurf running)
```

The `sync` command performs first-time setup automatically when needed:
- Local sources (claudecode, opencode): No setup required
- Web sources (claude, deepseek, chatgpt): See CDP setup below
- windsurf: Requires Windsurf to be running with Language Server active

### CDP setup

Web-based ingestors require a Chrome/Chromium browser with remote debugging enabled:

```sh
# Start Chrome with CDP (pick a free port)
google-chrome --remote-debugging-port=9222

# Or use flatpak (use isolated temp profile to avoid CDP issues):
flatpak run com.google.Chrome --remote-debugging-port=9222 --user-dir=/tmp/llm-archive-chrome
```

**Important:** 
- Use your normal browser profile so you're already logged in. Headless mode won't work due to Cloudflare CAPTCHA.
- Port 9222 is used by Windsurf CDP. For ChatGPT, use 9333 or another free port.

For CDP port conflicts:
```sh
google-chrome --remote-debugging-port=9333
```

### Force full resync

```sh
uv run llm-archive sync <source> -f    # force full resync (ignore last sync timestamp)
```

### Status

```sh
uv run llm-archive status    # per-source: threads, messages, last sync time
uv run llm-archive sources   # list all sources and initialization status
```

## MCP Server

llm-archive exposes an MCP (Model Context Protocol) server for querying your archive from any MCP-compatible client (Claude Code, Cline, Cursor, etc.).

```sh
llm-archive mcp    # start MCP server (stdio transport)
```

### Client configuration

Add to your MCP client config (e.g. `.mcp.json` or equivalent):

```json
{
  "llm-archive": {
    "command": "llm-archive",
    "args": ["mcp"]
  }
}
```

### Available tools

| Tool | Description |
|------|-------------|
| `search_conversations` | Full-text search across all messages (FTS5) |
| `search_threads` | Find conversations by topic, grouped by thread |
| `list_conversations` | List all threads sorted by recency |
| `get_conversation` | Retrieve full thread with all messages |
| `get_message` | Get a single message with parent thread context |
| `list_sources` | Show configured sources and last sync time |

## Database

Conversations are stored in `~/.llm-archive/archive.db` (SQLite, WAL mode).

```
sources   — configured sources + last sync timestamp
threads   — one row per conversation, with SHA1 for dedup
messages  — individual messages with role, content, metadata (model, tokens)
```

SHA1 dedup: repeated runs are safe — unchanged threads are skipped, updated threads are re-imported.

## Architecture

All sources implement a single `BaseIngestor` interface in `llm_archive/ingestors/base.py`.
Adding a new source = one new file in `llm_archive/ingestors/`, registered in `llm_archive/registry.py`.

```
llm_archive/
├── cli.py              # click commands: init, sync, status, sources, mcp
├── mcp_server.py       # MCP server (FastMCP, stdio transport)
├── db.py               # SQLite setup, SHA1 dedup, data access
├── schema.py           # IngestedThread, IngestedMessage dataclasses
├── registry.py         # INGESTORS dict
├── auth/
│   └── playwright.py   # headful/headless auth, storageState management
└── ingestors/
    ├── base.py         # BaseIngestor ABC
    ├── claudecode.py  # ~/.claude/projects/**/*.jsonl
    ├── opencode.py     # ~/.local/share/opencode/opencode.db
    ├── claude.py       # claude.ai REST API
    ├── deepseek.py     # deepseek.com REST API
    └── windsurf.py     # Language Server API (auto-detected port)
```

## Tests

```sh
uv run pytest tests/ -v
```
