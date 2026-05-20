# Roadmap

## Vision

Single source of truth for all LLM conversations — archive existing conversations from any tool, create new ones, search everything in one place. No split between "old chats I exported" and "current chat I'm in."

## Existing foundation

llm-archive already handles most of the hard parts. The roadmap extends it, doesn't rebuild it.

| Requirement | Existing llm-archive | Status |
|---|---|---|
| Message storage | `threads` + `messages` + `message_parts` tables, WAL mode, proper indexing | ✅ |
| Rich content model | `IngestedPart` with `kind`, `text`, `data`, `visible`, `searchable` — covers code, tool calls, reasoning, search results, citations, directives | ✅ |
| FTS5 search | `messages_fts` + `message_parts_fts` virtual tables with prefix matching | ✅ |
| Semantic search | `sqlite-vec` integration via `llm-archive embed` + `vec_threads` | ✅ |
| Short IDs | Base53 encoding for human-friendly references (`t5`, `m42`) | ✅ |
| Dedup | SHA1-based thread dedup, safe to re-run | ✅ |
| Provider ingestors | 6 working ingestors: claudecode, opencode, claude, deepseek, chatgpt, windsurf | ✅ |
| MCP server | FastMCP server with 6 tools (search, list, get, etc) | ✅ |
| TUI browser | Textual-based conversation browser with search, expand, detail view | ✅ |
| CLI | Click-based CLI with sync, search, show, status, embed commands | ✅ |
| Structured output | `--output-format json` on OpenCode's `run` command (NDJSON events) | ✅ |

**Why llm-archive is the right base:**

- **Schema is provider-agnostic** — threads/messages/parts work for both ingested and generated conversations. No schema change needed for inference.
- **IngestedParts already model tool calls, code, reasoning** — the rich content format from Phase 2 is just serializing what's already there.
- **FTS5 crosses all sources** — archived and inference conversations share the same search index. No merge step.
- **Short IDs work for inference threads too** — `t5` could be an archived thread, `t6` an inference thread.
- **Single binary, no Docker, no MongoDB** — matches the "local CLI tool" constraint. LibreChat would require running a full web stack.
- **Ingestors are extensible** — adding a CLI bridge or API bridge follows the same pattern as adding a new source ingestor.

## How it works

```
┌──────────────────────────────────────────────────────────────┐
│  llm-archive (single binary, local SQLite)                   │
│                                                              │
│  ┌──────────────────┐    ┌──────────────────────────────┐   │
│  │  INGEST           │    │  INFER                       │   │
│  │  ─────────        │    │  ─────────                   │   │
│  │  claudecode JSONL │    │  ┌──────────┐  ┌──────────┐ │   │
│  │  opencode DB      │    │  │ API      │  │ CLI      │ │   │
│  │  claude.ai API    │    │  │ bridge   │  │ bridge   │ │   │
│  │  deepseek API     │◄──►│  │ (key)    │  │ (subproc)│ │   │
│  │  chatgpt API      │    │  │          │  │          │ │   │
│  │  windsurf API     │    │  │ OpenAI   │  │ claude   │ │   │
│  │                   │    │  │ Anthropic│  │ opencode │ │   │
│  │  (import from     │    │  │ Ollama   │  │ codex    │ │   │
│  │   any JSON)       │    │  │ OpenRouter│  │ (TBD)   │ │   │
│  │                   │    │  └────┬─────┘  └──────────┘ │   │
│  │                   │    │       │                      │   │
│  │                   │    │  ┌────▼─────────────────┐    │   │
│  │                   │    │  │  HTTP proxy           │    │   │
│  │                   │    │  │  /v1/chat/completions │    │   │
│  │                   │    │  │  /v1/models           │    │   │
│  │                   │    │  │  (SSE stream +        │    │   │
│  │                   │    │  │   dual-write to DB)   │    │   │
│  │                   │    │  └────┬─────────────────┘    │   │
│  │  └────────┬─────────┘    └─────┼──────────────────────┘   │
│  │           │                    │                           │
│  │           ▼                    ▼                           │
│  │  ┌────────────────────────────────────────┐               │
│  │  │  SQLite (one schema for all)           │               │
│  │  │  sources | threads | messages | parts  │               │
│  │  │  FTS5 across everything                │               │
│  │  └────────────────┬───────────────────────┘               │
│  │                   ▼                                       │
│  │  ┌──────────────┐  ┌───────────┐  ┌──────────────────┐   │
│  │  │  TUI          │  │  MCP      │  │  HTTP proxy      │   │
│  │  │  browse/chat  │  │  server   │  │  → any OpenAI    │   │
│  │  │  fork/search  │  │  (stdio)  │  │    client        │   │
│  │  └──────────────┘  └───────────┘  └──────────────────┘   │
│  └──────────────────────────────────────────────────────────────┘
```

## Priorities

1. **Inference engine** — CLI bridges + API bridges + HTTP proxy. Chat + archive in one DB, use from any OpenAI-compatible client.
2. **Rich content rendering** — parse/serialize tool calls, code, reasoning in plain text. Blocks TUI and proxy response rendering.
3. **Chat TUI** — optional (proxy already enables any UI). Build only if terminal-native interface needed.
4. **MCP inference extension** — add infer/fork tools to existing MCP server. Use from Cline, Claude Code, etc.

---

## Phase 1: Inference engine

Add chat capability. New conversations stored alongside archived ones using the same schema.

### CLI bridge (subscription models)

Spawn provider CLIs as subprocess for models behind paywalls (Claude Pro, Codex, OpenCode with paid models):

| Tool | Output | Token-streaming | Session resume | Stdin input |
|------|--------|-----------------|----------------|-------------|
| Claude Code | `stream-json` NDJSON | ✅ token-level deltas | `--resume <id>`, `-c` | pipe or `--input-format stream-json` |
| OpenCode | NDJSON via `run --format json` | ❌ complete text per step | `--continue`, `--session <id>` | pipe to `run` |
| Codex CLI | TBD — not on system, validate later | — | — | — |

**Claude Code** — `-p --output-format stream-json --include-partial-messages`. Proven by [Goose project](https://github.com/block/goose/pull/7029) for persistent subprocess. Supports `--input-format stream-json` for bidirectional NDJSON.

**OpenCode** — `run --format json` emits NDJSON: `step_start`, `text`, `tool_use`, `step_finish`, `error`. `tool_use` bundles both input and output (state.input + state.output). No token-level streaming — complete text per step. Session resume via `--continue` or `--session`.

**Protocol**: buffer stdout, split on newline, parse each line as JSON. For Claude Code with `--input-format stream-json`, keep subprocess alive and send/receive NDJSON bidirectionally.

### Direct API bridge (API-key models)

- OpenAI-compatible (OpenAI, Ollama, OpenRouter, Groq, etc)
- Anthropic Messages API (for direct Claude API access)

No LiteLLM — overkill for single-user. OpenAI-compatible covers almost everything. Anthropic API for direct Claude. Add more only when requested.

### OpenAI-compatible HTTP proxy

Expose all bridges (CLI + API) as a standard OpenAI-compatible HTTP endpoint. This lets any OpenAI-compatible client (Open WebUI, MinimalChat, StatelessChatUI, Cline, Continue.dev, ChatGPT app custom endpoint) use llm-archive as backend.

```
                    ┌──────────────────┐
Client ──POST──────→│  /v1/chat/       │
(any OpenAI         │  completions     │
compatible)         │                  │
                    │  1. translate    │
                    │     OpenAI       │
                    │     schema →     │
                    │     unified      │
                    │     NDJSON       │
                    │  2. spawn bridge │
                    │  3. stream SSE   │
                    │  4. dual-write   │
                    │     to archive   │
                    └──────────────────┘
                         │
                    ┌────▼────┐
                    │ archive │
                    │ DB      │
                    └─────────┘

GET /v1/models → list available providers/models
```

**Dual-write**: during SSE streaming, every complete message gets written to llm-archive DB with `source_id = "inference"`. The client UI may store its own copy too — that's fine, llm-archive is the canonical store. No sync needed because writes happen in real-time during the request.

**Endpoints**:
- `POST /v1/chat/completions` — translate OpenAI request → unified NDJSON → bridge → translate response → SSE stream
- `GET /v1/models` — return list of configured providers/models

**Cost**: ~200-300 lines of FastAPI. Reuses the same bridge/engine code as CLI and TUI paths.

This replaces the need for a custom web UI — any existing OpenAI-compatible client works as frontend.

### Storage

Same schema as archive. New threads get `source_id = "inference"` with `provider` and `model` in metadata:

```
threads:  source_id="inference", provider="opencode", model="big-pickle"
messages: role, content, parts (same IngestedPart kinds)
```

### Closed loop (re-ingestion)

Inference conversations must be syncable back into the ingest pipeline. If you later switch to a different chat tool, the inference history is already in the DB — just re-index it. This means:

- Inference messages stored with the same schema as ingested ones
- `llm-archive sync` should skip `source_id = "inference"` (already local, no external source to pull from)

### Continue archived conversation

Flow for forking an archived conversation into inference:

1. User selects archived thread in browser
2. Pick "Continue here" → inference mode
3. Context assembly:
   - Load messages from DB up to context window limit
   - Flatten to plain text with content markers (see Phase 2)
   - Pass as user message(s) to provider CLI with continuation instruction
   - New messages stored as `source_id = "inference"`, linked by `forked_from: <thread_id>`
4. On finish, new thread is searchable alongside the original

### Cost tracking

Each `step_finish`/`result` event includes token counts. Store per-message:

```
messages.metadata: {
  "input_tokens": 11562,
  "output_tokens": 6,
  "cost": 0.00015,
  "model": "claude-sonnet-4"
}
```

Phase 1 **done when**: can send a prompt via CLI bridge, API bridge, and HTTP proxy — all store response in archive DB, visible in `llm-archive status` and in any OpenAI-compatible client.

---

## Phase 2: Rich content in plain text

No escape codes, no ANSI, no control characters. All structured content (code, tool calls, reasoning) represented in readable plain text.

| Content | Plain-text format |
|---------|------------------|
| code block | ```` ```python\nprint("hi")\n```` |
| tool call | `[tool_use: Bash] command: ls -la` |
| tool result | `[tool_result: exit=0] file1.txt\nfile2.txt` |
| reasoning | `[reasoning] ...` |
| thinking | `[thinking] ...` |
| search query | `[search: "how to..." ]` |
| search results | `[search_results] 3 results` |
| error | `[error: QuotaExceeded]` |

llm-archive's `IngestedPart` schema already has `kind`, `text`, `data`, `visible`, `searchable`. This phase adds a `to_plain_text()` serializer that converts any part to the format above.

Also needs a **parser** for the reverse direction: when continuing an archived conversation, parse the plain text back into structured messages for the provider API.

This phase blocks the Chat TUI — you can't display inference output without rendering tool calls and code blocks.

Phase 2 **done when**: `to_plain_text()` and `from_plain_text()` handle all `IngestedPart` kinds.

---

## Phase 3: Chat TUI

Extend existing Textual TUI from read-only browser to full chat interface.

### New elements

| Element | Source |
|---------|--------|
| Message input | already have `Input` widget in `tui.py` |
| Conversation list | existing `ListScreen`, add active chat indicator |
| Message stream render | new — append deltas as they arrive |
| Model/provider selector | new — pick bridge + model |
| New conversation | new — start fresh thread |

### States

- **Browse mode** (current) — scroll/search archived conversations
- **Chat mode** — active inference session. Input at bottom, streaming response above, scrollable history
- **Fork mode** — from browse, select "Continue here" → enters chat mode with preloaded context

### Abort/cancel

SIGINT to subprocess. If provider supports cancellation (Claude Code does via signal), propagate. Otherwise kill and call it done.

Phase 3 **done when**: can start a new chat, send message, stream response, fork from archive, all in the TUI.

---

## Phase 4: MCP inference extension

Expose inference through the existing MCP server (currently read-only: search, list, get). Add tools:

| Tool | Description |
|------|-------------|
| `infer` | Send prompt to a provider/model, stream response back |
| `infer_continue` | Continue existing inference thread |
| `infer_fork` | Fork archived conversation into inference |
| `list_providers` | Show configured providers and models |

MCP transport stays stdio by default. Can add `sse`/`streamable-http` later.

This lets any MCP client (Claude Code, Cline, Cursor) use llm-archive as both memory + inference backplane. Phase 4 is independent of Phase 3 — and alongside the HTTP proxy (Phase 1), covers all integration paths without a custom UI.

Phase 4 **done when**: MCP client can call `infer` and get a response.

---

## Phase 5: Replaced

OpenAI-compatible HTTP proxy (Phase 1) eliminates the need for a custom web UI. Any OpenAI-compatible client works as frontend — MinimalChat for PWA, StatelessChatUI for zero-install, Open WebUI for full-featured, Cline for IDE integration. llm-archive is the canonical store via dual-write; the client is just a view layer.

If later a custom UI is wanted (e.g. embedded conversation browser with archive search), build it as a thin FastAPI page using QuikChat widget or similar — ~200 lines, not a phase.

---

## Appendix A: Unified inference event stream

No single provider format exists. The inference bridge normalizes every provider into one NDJSON format.

### Provider event comparison

| Semantic event | OpenCode | Claude Code |
|---|---|---|
| step begin | `step_start` | `stream_event` / `content_block_start` |
| text output | `text` (complete) | `stream_event` / `content_block_delta` / `text_delta` |
| tool call input | `tool_use` (full `state.input`) | `content_block_start` (tool_use) + `input_json_delta` |
| tool result | `tool_use` (embedded `state.output`) | separate `assistant` message with `tool_result` |
| error | `error` (name + data) | `system` / `api_retry` |
| step end | `step_finish` (reason + tokens) | `message_delta` + `message_stop` / `result` |

### How tool calls are represented

**OpenCode** bundles input + output in one `tool_use` event. Unified parser splits:
```
→ {"type":"delta","kind":"tool_call","name":"Bash","input":{"command":"ls"}}
→ {"type":"delta","kind":"tool_result","call_id":"call_xxx","output":"file1.txt","exit":0}
```

**Claude Code** separates across messages. Unified parser maps directly:
```
→ {"type":"delta","kind":"tool_call","name":"Read","input":{"path":"/foo"}}
→ (later) {"type":"delta","kind":"tool_result","call_id":"toolu_...","output":"content..."}
```

### Unified NDJSON event types

```
type: "delta"       kind: "text"        text: "..."                                 # text chunk
type: "delta"       kind: "tool_call"   name: "Bash"  input: {"cmd":"ls"}           # tool call
type: "delta"       kind: "tool_result" call_id: "..."  output: "..."  exit: 0       # tool result
type: "delta"       kind: "reasoning"   text: "..."                                 # thinking
type: "delta"       kind: "code"        text: "..."  language: "python"              # code block
type: "stop"        reason: "stop|tool-calls|error"  tokens: {...}  cost: 0.002     # step end
type: "message"     role: "assistant|user"  content: [...]                          # complete message
type: "session"     session_id: "..."  model: "..."  provider: "..."                # session metadata
type: "error"       message: "..."                                                   # fatal error
```

### Bridge architecture

Bridges are stateless — stdin → unified NDJSON → stdout. The inference engine spawns the right one based on provider config.

```
provider config → picks bridge
     │
     ▼
┌────────────┐     stdin           ┌──────────────┐     stdout/unified     ┌────────────┐
│  claude     │───── NDJSON ──────→│  claude_bridge│───── NDJSON ─────────→│  archive   │
│  subprocess │                    │  .py / .rs   │                        │  engine    │
└────────────┘                     └──────────────┘                        └────────────┘
```

## Appendix B: OpenCode import/export bridge

OpenCode has native `export` and `import` commands. llm-archive can use these as additional paths:

- **Ingest**: `opencode export <sessionID>` → pipe into llm-archive as a new ingestor
- **Egress**: llm-archive thread → `opencode import` for when you want to move a conversation back to OpenCode

Same for Claude Code sessions if they support export. Makes the archive a hub rather than a sink.

## Appendix C: Error handling

| Error | CLI bridge | API bridge |
|-------|-----------|------------|
| Rate limited | wait and retry (claude auto-retries) | retry with backoff |
| Quota exceeded | report + stop | report + stop |
| Model unavailable | fallback to alternate | fallback to alternate |
| Subprocess crash | restart with same session ID | N/A |
| Connection lost | reconnect if API, fail if CLI | reconnect |
| Context too long | truncate oldest messages | truncate oldest messages |

Subprocess crash handling: on unexpected exit, check if we can resume via session ID. If yes, restart and continue. If no (session lost), report error with partial messages preserved in DB.

## Success criteria summary

| Phase | Done when |
|-------|-----------|
| 1: Inference engine | Can prompt via CLI bridge + API bridge + HTTP proxy, store response, see in `llm-archive status` and in any OpenAI-compatible client |
| 2: Rich plain text | `to_plain_text()` and `from_plain_text()` handle all `IngestedPart` kinds |
| 3: Chat TUI | Start new chat, send message, stream response, fork from archive |
| 4: MCP inference | MCP client can call `infer` tool and get response |
| 5: Replaced | Proxy eliminates need for custom web UI. Any OpenAI-compatible client works. |
