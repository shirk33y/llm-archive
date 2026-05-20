# Roadmap

## Vision

Single source of truth for all LLM conversations — archive + inference in one place. Browse, search, and continue any conversation regardless of provider.

## Phase 1: Inference Engine

Add chat capability to llm-archive so new conversations live alongside archived ones.

### CLI Bridge (subscription models)

Spawn provider CLIs as persistent subprocess for models behind paywalls (Claude Pro, Codex, etc):

- **Claude Code**: `--input-format stream-json --output-format stream-json` — NDJSON on stdin/stdout, resume by session ID via `--resume`
- **Codex CLI**: same protocol if supported, else stdin pipe
- **OpenCode**: stdin prompt, stdout stream
- Protocol: buffer stdout, split on newline, parse each line as NDJSON events

### Direct API (API-key models)

- OpenAI-compatible (OpenAI, Ollama, OpenRouter, etc)
- Anthropic Messages API
- LiteLLM as optional proxy layer

### Storage

Existing SQLite schema already handles threads + messages. Inference creates new threads with `source_id = "inference"` alongside archived sources (`claude`, `opencode`, etc). Dedup by SHA1 irrelevant here — all new data.

```
threads:  source_id="inference", provider="claude-code", model="claude-sonnet-4"
messages: role, content, parts (same schema as archive)
```

## Phase 2: Rich Content in Plain Text

No escape codes. No ANSI. Represent structured content as plain text:

| Content | Plain-text Format |
|---------|------------------|
| code block | ```` ```lang\ncode\n```` |
| tool call | `[tool_use: Read] path/to/file` |
| tool result | `[tool_result: exit=0] output...` |
| reasoning | `[reasoning] ...` |
| thinking | `[thinking] ...` |
| search | `[search: query]` |

This is what llm-archive's `IngestedPart` schema already models — just needs a serialization pass for the inference path.

## Phase 3: Chat TUI

Extend existing Textual TUI (`tui.py`) from read-only browser to full chat interface:

- Input box at bottom (already have Input widget)
- Send message → inference engine → stream response into message list
- Model/provider selector
- Conversation switcher (existing list screen)
- Fork archived conversation → continue in inference mode

## Phase 4: Web UI (if needed)

Only if TUI isn't enough. Options:

- **Lightweight**: extend MCP server with HTTP transport, pair with minimal web frontend
- **Full**: FastAPI backend (reuse inference engine) + React/Svelte frontend
- **Not**: LibreChat — wrong data model, wrong DB, wrong architecture for this

## Non-goals

- Multi-user auth — single-user tool
- RBAC, orgs, teams
- Docker deployment — local CLI tool
- Plugin ecosystem
- Image generation or voice

## Design principles

- **Plain text first** — no escape codes from CLI bridge. Strip on read.
- **Subprocess over SDK** — CLI bridge avoids API key management for subscription models. SDK path available as alternative.
- **Archive and inference share schema** — one DB, one search, one UI.
- **Provider-agnostic storage** — thread + message model is generic enough.
