# llm-archive

A CLI tool that dumps and syncs AI conversations from multiple sources into a local SQLite database.

## Supported sources

| Source | Status | Method |
|--------|--------|--------|
| `claude_code` | ✅ working | local JSONL (`~/.claude/projects/`) |
| `opencode` | ✅ working | local SQLite (`~/.local/share/opencode/opencode.db`) |
| `claude` | ✅ working | claude.ai REST API + Playwright auth |
| `windsurf` | ⚠️ blocked | `.pb` files are encrypted — cannot parse |
| ChatGPT | 🔜 planned | REST API |
| DeepSeek | 🔜 planned | REST API |

## Installation

```sh
git clone https://github.com/shirk33y/llm-archive
cd llm-archive
uv venv && uv sync
```

## Usage

### First-time import

```sh
uv run llm-archive init claude_code   # imports all Claude Code sessions
uv run llm-archive init opencode      # imports all OpenCode sessions
uv run llm-archive init claude        # opens browser → log in → dumps all claude.ai conversations
```

### Incremental sync

```sh
uv run llm-archive sync               # sync all sources
uv run llm-archive sync claude_code   # sync one source
```

### Status

```sh
uv run llm-archive status    # per-source: threads, messages, last sync time
uv run llm-archive sources   # list all sources and initialization status
```

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
├── cli.py              # click commands: init, sync, status, sources
├── db.py               # SQLite setup, SHA1 dedup, data access
├── schema.py           # IngestedThread, IngestedMessage dataclasses
├── registry.py         # INGESTORS dict
├── auth/
│   └── playwright.py   # headful/headless auth, storageState management
└── ingestors/
    ├── base.py         # BaseIngestor ABC
    ├── claude_code.py  # ~/.claude/projects/**/*.jsonl
    ├── opencode.py     # ~/.local/share/opencode/opencode.db
    ├── claude.py       # claude.ai REST API
    └── windsurf.py     # scaffolded (encrypted .pb, WIP)
```

## Tests

```sh
uv run pytest tests/ -v
```
