# TODO

- [ ] Extract sync logic from `llm_archive/ingestors/chatgpt.py` to `sync.py` and apply to other ingestors
- [ ] Add VS Code Copilot ingestor
- [ ] Ask user before auto-launching CDP browser (currently opens Chrome silently)
- [ ] Configurable Chrome/Chromium path
- [ ] Auto-download Chromium and launch it if no browser found
- [ ] Explore embedded webview (e.g. pywebview) — keeps browser session alive, avoids CDP auto-close, self-contained login flow
- [ ] `llm-archive resume <source> <thread_id>` — hint or subcommand to resume a session in its original tool (e.g. `opencode run --session <id>`)
- [ ] Summarize threads to db using cli tools like claude, opencode, ollama
- [ ] Display resume URL or command
- [ ] Explore sqlite-vec for semantic search
- [ ] Design TUI
- [ ] Investigate openchronicle
- [ ] Store/download images and other artifacts uploaded or produced by AI
