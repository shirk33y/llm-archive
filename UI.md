# Web UI research

Trigger conditions per ROADMAP (only build if any met):
- Need archive on phone/tablet
- Need shareable URLs
- TUI degrades at >5000 conversations

None triggered yet. This doc is reconnaissance for when needed.

## Option 1: FastAPI + bare HTML/JS (~500 lines)

Wraps existing llm-archive API directly:

```
FastAPI server → llm-archive.db (read/write)
HTML/JS SPA (inline, no build step)
```

- **Pros**: Minimal deps, single binary if bundled with static assets, reuse existing MCP server layer
- **Cons**: No reactivity framework, hand-rolled JS, harder to maintain over time

## Option 2: FastAPI + SolidJS/Svelte

Rich frontend, same backend pattern:

```
FastAPI → llm-archive.db
SolidJS/Svelte SPA (Vite build)
```

- **Pros**: Component model, reactive state, good DX
- **Cons**: Build step, larger surface, npm dependency

## Option 3: Open WebUI (rejected)

Python/FastAPI + SvelteKit, 138K GitHub stars.

- **Architecture**: FastAPI backend, SvelteKit 5 frontend, SQLite/PostgreSQL, Socket.IO for streaming
- **Extensibility**: Python Tools & Functions (in-process), MCP (Streamable HTTP), OpenAPI import, Pipeline workers
- **MCP**: Native Streamable HTTP only. stdio requires mcpo proxy.
- **Deployment**: Docker-only in practice, multi-user with auth/RBAC
- **Why not**: Docker dependency, multi-user bloat, Postgres overkill for single-user local tool, different DB schema

## Option 4: OpenCode Web UI (rejected)

SolidJS SPA + Effect/HttpApi Go-style backend (TypeScript on Bun).

### API surface (v1 + v2)

| Group | Endpoints |
|-------|-----------|
| Session | `GET/POST/DELETE /session`, `/session/:id`, `/session/:id/message`, `/session/:id/fork`, `/session/:id/share`, `/session/:id/abort`, `/session/:id/revert`, `/session/:id/summarize` |
| API Session (v2) | `GET /api/session`, `POST /api/session/:id/prompt`, `/api/session/:id/compact`, `/api/session/:id/wait`, `/api/session/:id/context` |
| Provider | `GET /provider`, `/provider/:id/oauth/*` |
| API Provider (v2) | `GET /api/provider`, `/api/provider/:id` |
| MCP | `GET/POST/DELETE /mcp`, `/mcp/:name/auth/*`, `/mcp/:name/connect`, `/mcp/:name/disconnect` |
| File | `GET /file`, `/file/content`, `/file/status` |
| Find | `GET /find`, `/find/file`, `/find/symbol` |
| Config | `GET/PATCH /config`, `/config/providers` |
| Workspace | `GET/POST/DELETE /experimental/workspace/*` |
| PTY | WebSocket `/pty/:id/connect` |
| Events | SSE `/event`, `/global/event` |
| TUI | `/tui/*` control routes |

### Why not

Codebase is 30K+ lines of Effect TypeScript across 9 packages. The web UI is tightly coupled to OpenCode's own:
- **Session model**: tied to project directories, workspaces, agent runtime, Crush-based agent protocol
- **Provider/model**: OpenCode-specific config schema and auth flow
- **Workspace**: multi-repo workspace management with git worktrees
- **Auth**: OpenCode console auth, OAuth for providers
- **TUI bridge**: `/tui/*` routes are TUI-specific, not general API

The API surface is rich (sessions, providers, MCP, files, events, PTY) but every endpoint assumes OpenCode's data model underneath. Would need to either fork and replace the entire backend or run the full OpenCode server as middleware — both worse than building from scratch.

Key insight: **OpenCode's web UI is its own agent frontend, not a generic chat UI**. The session concept is "one agent conversation per project directory" with workspace routing, not "one conversation thread in a flat database."

## Recommendation

1. **Now**: Skip web UI. Focus on Phase 1-4 (Inference → Rich Text → Chat TUI → MCP).
2. **When triggered**: Option 1 (FastAPI + bare HTML/JS). 500 lines, reuses existing MCP server code path, minimal maintenance surface.
3. **If** triggered and more resources: Option 2 (FastAPI + SolidJS/Svelte).

Open WebUI is overengineered for single-user local tool with SQLite. OpenCode web UI is too coupled to its own agent protocol.
