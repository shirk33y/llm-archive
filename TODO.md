# TODO

- [ ] Extract sync logic from `llm_archive/ingestors/chatgpt.py` to `sync.py` and apply to other ingestors
- [ ] Ask user before auto-launching CDP browser (currently opens Chrome silently)
- [ ] Configurable Chrome/Chromium path
- [ ] Auto-download Chromium and launch it if no browser found
- [ ] Explore embedded webview (e.g. pywebview) — keeps browser session alive, avoids CDP auto-close, self-contained login flow
