# llm-archive

A CLI tool that dumps and syncs AI conversations from multiple sources into a local SQLite database.

## Supported sources

| Source | Status | Method |
|--------|--------|--------|
| `claudecode` | ✅ working | local JSONL (`~/.claude/projects/`) |
| `opencode` | ✅ working | local SQLite (`~/.local/share/opencode/opencode.db`) |
| `claude` | ✅ working | claude.ai REST API + Playwright auth |
| `deepseek` | ✅ working | deepseek.com REST API + Playwright auth |
| `windsurf` | ✅ working | local Language Server API (auto-detected port) |
| ChatGPT | 🔜 planned | REST API |

## Installation

```sh
git clone https://github.com/shirk33y/llm-archive
cd llm-archive
uv venv && uv sync
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
- Local sources (claude_code, opencode): No setup required
- API sources (claude, deepseek): Opens browser for first-time auth, then stores token
- windsurf: Requires Windsurf to be running with Language Server active

### Force full resync

```sh
uv run llm-archive sync <source> -f    # force full resync (ignore last sync timestamp)
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
    ├── deepseek.py     # deepseek.com REST API
    └── windsurf.py     # Language Server API (auto-detected port)
```

## Tests

```sh
uv run pytest tests/ -v
```
