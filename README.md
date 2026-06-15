# llm-archive

Local archive for AI chats. Syncs web and file-based providers into SQLite, then searches them from CLI, TUI, or MCP.

## Quickstart

```sh
brew tap shirk33y/llm-archive
brew install llm-archive --HEAD
llm-archive enable chatgpt
llm-archive sync chatgpt
llm-archive search "that thing I forgot"
```

Run background sync:

```sh
brew services start llm-archive
llm-archive status
```

## Providers

| Provider | Type | Auth/data |
| --- | --- | --- |
| `chatgpt` | web | browser cookies, fallback CDP |
| `claude` | web | browser cookies, fallback CDP |
| `deepseek` | web | browser cookies + localStorage token, fallback CDP |
| `claudecode` | file | local JSONL |
| `codex` | file | local JSONL |
| `opencode` | file | local SQLite |
| `windsurf` | file/API | local app API |

Web providers default to one-minute sync. File providers default to file watching plus a one-second minimum sync interval.

## Setup

```sh
llm-archive enable chatgpt
llm-archive enable claude
llm-archive enable deepseek
llm-archive enable claudecode
llm-archive disable deepseek
```

`enable` detects supported browser profiles and asks which one to use when more than one works. Supported browser families include Firefox/Waterfox/LibreWolf and Chromium-family browsers such as Chrome, Chromium, Brave, Edge, and Opera.

Config lives in the standard user config directory:

It is created automatically on first run with safe disabled defaults and any obvious browser profile root.

```toml
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
llm-archive enable <provider>
llm-archive disable <provider>
llm-archive sync [provider] [--force]
llm-archive search [--provider provider] <phrase>
llm-archive show <thread>
llm-archive status [--verbose]
llm-archive logs [provider]
llm-archive backup [--verify]
llm-archive service
llm-archive mcp
```

`service` runs the scheduler in the foreground. Homebrew runs it with `brew services`; no extra service subcommands.

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
