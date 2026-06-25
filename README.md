# llm-archive

Local archive for AI chats. Syncs web and file-based providers into SQLite, then searches them from CLI, TUI, or MCP.

Related project: [`unbalancedparentheses/llm-archive`](https://github.com/unbalancedparentheses/llm-archive), an analytics-oriented archive for Claude Code and Codex conversation history.

## Quickstart

**Homebrew** (macOS, Linux):

```sh
brew tap shirk33y/llm-archive https://github.com/shirk33y/llm-archive
brew trust --formula shirk33y/llm-archive/llm-archive
brew install llm-archive
```

**pipx** (uses your existing Python ≥ 3.11):

```sh
pipx install git+https://github.com/shirk33y/llm-archive.git
```

Then:

```sh
llm-archive enable chatgpt claude codex
llm-archive sync
llm-archive search "that thing I forgot"
```

Run background sync:

```sh
llm-archive service install
llm-archive service start
llm-archive status
```

## Providers

| Provider | Type | Auth/data |
| --- | --- | --- |
| `chatgpt` | web | browser cookies |
| `claude` | web | browser cookies |
| `deepseek` | web | browser cookies + localStorage token |
| `claudecode` | file | local JSONL |
| `codex` | file | local JSONL |
| `cursor` | file | local JSONL |
| `gemini` | file | local JSON |
| `opencode` | file | local SQLite |
| `windsurf` | file/API | local app API |

Web providers default to one-minute sync. File providers default to file watching plus a one-second minimum sync interval.

## Setup

```sh
llm-archive enable chatgpt claude deepseek
llm-archive enable claudecode codex
llm-archive disable deepseek cursor
```

`enable` detects supported browser profiles and asks which one to use when more than one works. File providers use their default data path automatically — pass `--path` to override. Supported browser families include Firefox/Waterfox/LibreWolf and Chromium-family browsers such as Chrome, Chromium, Brave, Edge, and Opera.

Config lives in the standard user config directory and is created automatically on first run with safe disabled defaults. The `[embed]` section controls auto-embedding after sync (on by default).

```toml
[embed]
auto = true

[ingestors.chatgpt]
enabled = true
mode = "cookies"
browser = "firefox"
browser_dir = "<browser-profile>"
sync_interval = "1m"
min_sync_interval = "1m"

[ingestors.claudecode]
enabled = true
watch = true
sync_interval = "1s"
min_sync_interval = "1s"
```

Durations use `ms`, `s`, `m`, `h`, or `d`.

## Commands

```sh
llm-archive enable <provider> [<provider> ...]
llm-archive disable <provider> [<provider> ...]
llm-archive sync [<provider> ...] [--force]
llm-archive embed [--force] [<provider>]
llm-archive search [--sync] [-s] [--provider provider] <phrase>
llm-archive resume <thread-id>
llm-archive show <thread>
llm-archive tui
llm-archive status [--verbose]
llm-archive logs [provider]
llm-archive backup [--verify]
llm-archive service
llm-archive service install|start|stop|restart|status|logs|uninstall
llm-archive mcp
```

`service` runs the scheduler in the foreground. `service install/start/stop/restart/status/logs/uninstall` delegates to `brew services` when run from the Homebrew package. Other installs create a native user service: systemd on Linux, launchd on macOS.

## Semantic search

`search -s` uses vector similarity (fastembed BAAI/bge-small-en-v1.5, 384d) via sqlite-vec. Embeddings are generated **automatically after every sync** by default — no manual step needed. You can also run:

```sh
llm-archive embed            # embed all unembedded threads
llm-archive embed --force    # re-embed everything
```

To disable auto-embedding, set `auto = false` in the `[embed]` config section. If the embedding model dimensions change, `llm-archive embed` will warn you and require `--force` to rebuild.

## Freshness

Sync has job locking and throttling. A CLI, MCP, or service-triggered sync joins an already-running sync for the same provider. Search can trigger a stale provider early, but still respects the provider minimum sync interval.

`status --verbose` shows service heartbeat, provider freshness, auth/path health, backup state, recent jobs, and setup hints.

## Development

```sh
uv sync --extra dev
uv run pytest -q
uv run ruff check
uv run pre-commit run --all-files
```

Pre-commit runs both lint and unit tests.
