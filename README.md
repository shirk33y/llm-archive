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
llm-archive start --install
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
llm-archive logs [-n N] [-f]
llm-archive backup [--verify]
llm-archive start [--install]
llm-archive stop [--uninstall]
llm-archive restart
llm-archive service              # run scheduler in foreground (used by service units)
llm-archive mcp
```

### Service control

`start`, `stop`, and `restart` manage the scheduler service. `start --install` registers it first (then starts); `stop --uninstall` stops and unregisters it. A bare `start` on an unregistered install prints a hint instead of silently registering:

```sh
llm-archive start --install      # register (if needed) and start
llm-archive start                # start; hints to use --install if not registered
llm-archive stop                 # stop, keep registration
llm-archive stop --uninstall     # stop and unregister/remove the unit
llm-archive restart
llm-archive logs -n 200 -f       # tail scheduler process logs
```

From the Homebrew package these delegate to `brew services`. Other installs create a native user service: systemd on Linux, launchd on macOS. `status` shows the service heartbeat alongside provider/freshness state.

### Dev mode

Reload the running scheduler on code/config changes without reinstalling the package. Useful for editable/checkout installs:

```toml
[dev]
watch = true
debounce_ms = 1000
gate = true                       # run `ruff check .` before each reload
gate_command = "ruff check . && pyright"   # optional custom gate (shell)
watch_paths = ["../sibling-repo"] # optional extra paths
```

When `watch = true`, the foreground scheduler watches the installed package source and config file. After changes settle for `debounce_ms`, it runs the gate; the process only reloads (`os.execv`) when the gate passes, so broken code never replaces a running service. Works best with an editable install (`pipx install -e`, `uv sync`) so the watched path is your checkout.

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
