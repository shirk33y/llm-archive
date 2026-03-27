# llm-archive

A CLI tool that dumps and syncs AI conversations from multiple sources into a local SQLite database.

## Supported sources

| Source | Status | Method |
|--------|--------|--------|
| Claude Code | planned | local JSONL (`~/.claude/projects/`) |
| OpenCode | planned | local files (`~/.local/share/opencode/`) |
| Claude.ai | planned | REST API + Playwright auth |
| Windsurf | planned | local files |
| ChatGPT | coming soon | REST API |
| DeepSeek | coming soon | REST API |

## Usage

```sh
llm-archive init <source>     # first-time setup
llm-archive sync [source]     # incremental sync
llm-archive status            # show counts and last sync times
llm-archive sources           # list configured sources
```

## Installation

```sh
pip install llm-archive
```

## Development

```sh
git clone https://github.com/shirk33y/llm-archive
cd llm-archive
pip install -e ".[dev]"
```

## Architecture

All sources implement a single `BaseIngestor` interface — adding a new source requires creating one file in `llm_archive/ingestors/`.
