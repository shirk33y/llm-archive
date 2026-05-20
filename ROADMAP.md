# Roadmap

Single source of truth for all LLM conversations — archive from any tool, generate new ones, search everything in one place. No split between "old chats I exported" and "current chat I'm in."

## Foundation

llm-archive already has: schema (threads+messages+parts), FTS5 across everything, sqlite-vec semantic search, Base53 short IDs, SHA1 dedup, 6 provider ingestors, FastMCP server (6 tools), Textual TUI, Click CLI. All we do is extend.

**Why extend vs rebuild**: schema is provider-agnostic (archived + inference share it), IngestedParts already model tool calls/code/reasoning, FTS5 crosses all sources, single binary/ no Docker/ no MongoDB, ingestors are extensible.

## Architecture

Ingest → SQLite ← Infer bridges (API key / CLI subprocess / HTTP proxy)
Three access paths: TUI (browse/chat), MCP server (stdio), HTTP proxy (any OpenAI client)

## Phases

1. **Inference engine** — CLI + API bridges + HTTP proxy
2. **Rich content** — parse/serialize tool calls, code, reasoning in plain text
3. **Chat TUI** — optional (proxy already enables any client)
4. **MCP inference** — add infer/fork tools to MCP server

---

### Phase 1: Inference engine

Chat capability. New conversations stored alongside archived ones, same schema.

**CLI bridge** (subscription models — spawn provider CLIs as subprocess):

| Tool | Output | Token-streaming | Resume |
|------|--------|-----------------|--------|
| Claude Code | `stream-json` NDJSON | ✅ per-delta | `--resume <id>` |
| OpenCode | `run --format json` NDJSON | ❌ per-step | `--session <id>` |
| Codex CLI | TBD — not on system | — | — |

Protocol: buffer stdout, split on newline, parse JSON. For Claude Code with `--input-format stream-json`, keep subprocess alive for bidirectional NDJSON.

**API bridge** (API-key models): OpenAI-compatible (OpenAI, Ollama, OpenRouter, Groq) + Anthropic Messages API. No LiteLLM — overkill for single-user.

**HTTP proxy** (OpenAI-compatible, ~200-300 lines FastAPI):

```
POST /v1/chat/completions → translate OpenAI schema → unified NDJSON → bridge → SSE stream + dual-write to DB
GET /v1/models → list providers/models
```

Dual-write: every complete message written to archive DB during SSE streaming (`source_id = "inference"`). The client UI may store its own copy — llm-archive is canonical. No sync needed.

**Storage**: new threads get `source_id="inference"`, messages use same IngestedPart kinds. Metadata includes provider, model, token counts, cost. Fork from archive: load context from DB (up to window limit), flatten to plain text, pass as continuation, store new messages linked by `forked_from`.

**Done**: prompt via CLI bridge + API bridge + HTTP proxy, response stored in archive, visible in `llm-archive status` and any OpenAI-compatible client.

---

### Phase 2: Rich content in plain text

No escape codes, no ANSI. All structured content in readable plain text:

| Content | Format |
|---------|--------|
| code | ```` ```python\nprint("hi")\n```` |
| tool call | `[tool_use: Bash] command: ls -la` |
| tool result | `[tool_result: exit=0] file1.txt` |
| reasoning | `[reasoning] ...` |
| search | `[search: "query"]` / `[search_results] N results` |
| error | `[error: QuotaExceeded]` |

Adds `to_plain_text()` serializer on IngestedPart (already has `kind`, `text`, `data`, `visible`, `searchable`) and `from_plain_text()` parser for reverse direction (continue archived conversation).

Blocks TUI + proxy — can't render inference output without handling tool calls and code blocks.

**Done**: `to_plain_text()` and `from_plain_text()` handle all IngestedPart kinds.

---

### Phase 3: Chat TUI

Extend existing Textual TUI from read-only browser to full chat. Three modes: Browse (current), Chat (streaming response, input at bottom), Fork (preload archived context, enter chat). Depends on Phase 2 for rendering. SIGINT to abort.

**Optional** — proxy already enables any OpenAI-compatible client. Build only if terminal-native interface needed.

**Done**: start new chat, send message, stream response, fork from archive, all in TUI.

---

### Phase 4: MCP inference extension

Add to existing MCP server (currently read-only: search, list, get):

- `infer` — prompt a provider/model, stream response
- `infer_continue` — continue inference thread
- `infer_fork` — fork archived conversation into inference
- `list_providers` — configured providers and models

Transport stays stdio; can add SSE/streamable-http later. Lets any MCP client (Cline, Claude Code, Cursor) use llm-archive as memory + inference backplane.

**Done**: MCP client can call `infer` and get a response.

### Phase 5: Replaced

HTTP proxy (Phase 1) eliminates need for custom web UI. Any OpenAI-compatible client works — MinimalChat (PWA), StatelessChatUI (zero-install), Open WebUI (full-featured), Cline (IDE). If later a custom browser for archive search is wanted, build as thin FastAPI page ~200 lines, not a phase.

---

## Appendix: Unified inference event stream

Bridges normalize every provider into one NDJSON format consumed by the archive engine:

```
type: "delta"   kind: "text"        text: "..."                   # text chunk
type: "delta"   kind: "tool_call"   name: "Bash"  input: {...}    # tool call
type: "delta"   kind: "tool_result" call_id: "..."  output: "..." # tool result
type: "delta"   kind: "reasoning"   text: "..."                   # thinking
type: "delta"   kind: "code"        text: "..."  lang: "python"   # code block
type: "stop"    reason: "stop|tool-calls|error"  tokens cost:0.00 # step end
type: "message" role: "assistant|user"  content: [...]            # complete msg
type: "session" session_id model provider                         # metadata
type: "error"   message: "..."                                    # fatal
```

Provider-specific mappings:

| Event | OpenCode | Claude Code |
|-------|----------|-------------|
| step begin | `step_start` | `content_block_start` |
| text | `text` (complete) | `content_block_delta.text_delta` |
| tool call | `tool_use` (state.input) | `content_block_start`(tool_use) + `input_json_delta` |
| tool result | `tool_use` (state.output) | separate `assistant` msg with `tool_result` |
| error | `error` | `system` / `api_retry` |
| step end | `step_finish` | `message_delta` + `message_stop` |

**Key difference**: OpenCode bundles tool call + result in one event; Claude Code separates across messages. Unified parser normalizes both into the delta stream above.

## Appendix: Error handling

| Error | CLI bridge | API bridge |
|-------|-----------|------------|
| Rate limited | wait+retry (claude auto) | retry backoff |
| Quota exceeded | report+stop | report+stop |
| Model unavailable | fallback | fallback |
| Subprocess crash | restart if resumable, else report | N/A |
| Context too long | truncate oldest msgs | truncate oldest msgs |

## Done

| Phase | Done when |
|-------|-----------|
| 1: Inference | Prompt via CLI + API + HTTP proxy, stored in archive, visible in any client |
| 2: Rich text | `to_plain_text()` + `from_plain_text()` handle all IngestedPart kinds |
| 3: Chat TUI | Start chat, send msg, stream response, fork from archive |
| 4: MCP infer | MCP client calls `infer` and gets response |
