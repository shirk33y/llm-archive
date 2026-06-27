# TODO

- [ ] Add griffinmartin/opencode-claude-auth plugin for subscription OAuth (Linux, Windows optional)
- [ ] Extract sync logic from `llm_archive/ingestors/chatgpt.py` to `sync.py` and apply to other ingestors
- [ ] Add VS Code Copilot ingestor
- [x] ~~Ask user before auto-launching CDP browser~~ (obsolete — CDP removed)
- [x] ~~Configurable Chrome/Chromium path~~ (obsolete — CDP removed)
- [x] ~~Auto-download Chromium and launch it if no browser found~~ (obsolete — CDP removed)
- [x] ~~Explore embedded webview~~ (obsolete — CDP removed)
- [ ] `llm-archive resume <source> <thread_id>` — hint or subcommand to resume a session in its original tool (e.g. `opencode run --session <id>`)
- [x] ~~Summarize threads to db using cli tools like claude, opencode, ollama~~ (`llm-archive sum`)
- [ ] Add message references to thread summaries (which messages/part of conversation each summary tier covers, so MCP client can dig into source)
- [ ] Display resume URL or command
- [ ] Explore sqlite-vec for semantic search
- [ ] Design TUI
- [ ] Investigate openchronicle
- [ ] Store/download images and other artifacts uploaded or produced by AI
- [ ] Auto route requests through multiple APIs: https://github.com/workweave/router

## Ideas from `unbalancedparentheses/llm-archive`

- [ ] Add activity analytics commands: `hours`, `projects`, `timeline`, and `day`
- [ ] Add bulk export command for Markdown and JSON conversations
- [ ] Add optional idea mining over recent conversations, excluding tool noise and directives
- [ ] Add optional weekly/recent summary generation with local Ollama fallback and cost confirmation
- [ ] Add normalized token usage and cost reporting per provider, project, and time window
- [ ] Add recurring-question detection for repeated user prompts or unresolved problems
- [ ] Add lightweight topic extraction over searchable message parts, scoped by source/project/date
