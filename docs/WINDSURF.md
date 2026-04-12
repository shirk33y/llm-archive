# Windsurf Cascade Conversation Extraction

This document summarizes findings from reverse engineering Windsurf's conversation storage and the implemented solution for programmatic access.

## Storage Overview

Windsurf stores conversation data in multiple locations using different formats:

### 1. Encrypted Persistent Storage
- **Location**: `~/.codeium/windsurf/cascade/*.pb`
- **Format**: Encrypted Protocol Buffers (protobuf)
- **Status**: Not directly parseable without encryption key

### 2. LocalStorage Session Index
- **Key**: `cascade-open-sessions-by-workspace`
- **Format**: JSON
- **Content**: Workspace-scoped session metadata including tab IDs and active session

### 3. In-Memory React State (CDP Accessible)
- **Location**: React fiber tree inside `windsurf.cascadePanel` DOM element
- **Key**: `props.trajectory` on React component
- **Format**: JavaScript objects with protobuf-like structure

## Encryption Details

Windsurf uses Electron's `safeStorage` API for encrypting `.pb` files:

| Platform | Key Provider | Notes |
|----------|--------------|-------|
| macOS | Keychain Access | App-specific keys, user protection |
| Windows | DPAPI | User-scoped protection |
| Linux | Secret Service (GNOME/KWallet) or `basic_text` fallback | May use hardcoded password if no secret store |

**Key insight**: On Linux without a secret store, `safeStorage.getSelectedStorageBackend()` returns `basic_text`, indicating potential vulnerability.

## CDP Extraction Method (Implemented)

The working solution connects to Windsurf via Chrome DevTools Protocol and extracts conversations from the React component tree.

### Connection
```bash
# Start Windsurf with remote debugging
windsurf --remote-debugging-port=9222 --remote-allow-origins='*'

# Check CDP endpoint
curl http://localhost:9222/json/list
```

### Data Structure

The `trajectory` object contains a `steps` array with the following step types:

#### `userInput`
- `userResponse`: User text input
- `items`: Array of input items (for multi-part inputs)

#### `plannerResponse`
- `response`: AI's response text
- `thinking`: Chain-of-thought reasoning (often hidden)

#### `runCommand`
- `command`: Command name
- `args`: Command arguments
- `stdout`/`stdoutBuffer`: Command output
- `exitCode`: Exit status

#### `writeFile`
- `path`/`filePath`: Target file path
- `content`: File content

#### `readFile`
- `path`/`filePath`: Source file path

#### `todoList`
- `todos`: Array of todo items with `content` field

#### `checkpoint`
- `userIntent`: User's stated intent for this phase

### Extraction Script

The `-cascade-history` script (located at `~/bin/-cascade-history`) provides:
- Auto-restart Windsurf with CDP if not running
- List all sessions via localStorage
- Extract individual conversations by clicking through sessions
- Output to JSON or formatted text

## Alternative Approaches

### 1. Source Code Extraction
Electron apps store code in ASAR archives:
```bash
npx asar extract /usr/share/windsurf/resources/app.asar ./extracted
```
This could reveal `.proto` definitions and encryption logic.

### 2. Memory Dump
Since conversations are decrypted in memory for display, attaching a debugger to the renderer process could yield plaintext data.

### 3. API Interception
Windsurf syncs to `*.codeium.com` and `*.windsurf.com` endpoints. Intercepting these with mitmproxy could capture conversation data in transit.

### 4. IndexedDB/LevelDB
Chromium-based apps use LevelDB for IndexedDB storage:
- Location: `~/.config/Windsurf/IndexedDB/` (non-Flatpak)
- Location: `~/.var/app/<app-id>/config/IndexedDB/` (Flatpak)
- Tools: [ccl_chromium_reader](https://github.com/cclgroupltd/ccl_chromium_reader)

## Implementation

The `WindsurfIngestor` class in `llm_archive/ingestors/windsurf.py` implements CDP-based extraction:

```python
from llm_archive.ingestors import get_ingestor

# Auto-restart Windsurf with CDP if not running
ingestor = get_ingestor("windsurf")
await ingestor.init(auto_restart=True)

# Count available sessions
count = await ingestor.count_threads()
print(f"Found {count} sessions")

# Extract all conversations
async for thread in ingestor.threads():
    print(f"{thread.title}: {len(thread.messages)} messages")
```

### Features

- **Auto-restart**: Automatically kills and restarts Windsurf with CDP enabled
- **Multi-platform launch**: Supports direct binary, Flatpak, and Distrobox
- **Session enumeration**: Counts and extracts all available Cascade sessions
- **Full conversation extraction**: Captures user messages, AI responses, commands, file operations

### Requirements
- `websocket-client` package (added to `pyproject.toml`)
- Windsurf installed (auto-detected via `windsurf`, `flatpak run com.codeium.Windsurf`, or `distrobox enter windsurf`)

### CLI Usage

```bash
# Auto-restart Windsurf with CDP if not running
uv run llm-archive sync windsurf --restart

# Manual start with CDP already enabled
windsurf --remote-debugging-port=9222
uv run llm-archive sync windsurf

# Custom database path
uv run llm-archive sync windsurf --restart --db-path ~/my-archive.db
```

**Note:** The `sync` command performs first-time setup automatically. No separate `init` needed. Use `--restart` to auto-restart Windsurf with CDP enabled.

### Message Mapping

| Windsurf Step | Role | Parts |
|---------------|------|-------|
| `userInput` | `user` | `text` |
| `plannerResponse` | `assistant` | `thinking` (hidden), `text` |
| `runCommand` | `tool` | `tool_call` with command/stdout |
| `writeFile` | `tool` | `tool_call` with path |
| `readFile` | `tool` | `tool_call` with path |
| `todoList` | `tool` | `tool_call` with todos |
| `checkpoint` | `system` | `system` (hidden) |

## Known Limitations

1. **Requires running Windsurf**: Cannot extract from `.pb` files directly
2. **Session switching**: Must click through each session to extract all conversations (may be flaky)
3. **Real-time only**: Historical conversations not in the current session list require scrolling/loading in UI
4. **CDP port conflicts**: If port 9222 is in use, manually specify alternate port

## References

- Reddit discussions: r/Codeium and r/windsurf
- [ccl_chromium_reader](https://github.com/cclgroupltd/ccl_chromium_reader) - Chromium data extraction
- [getspecstory](https://github.com/specstoryai/getspecstory) - Third-party conversation export tool (Windsurf support pending)
- [Electron safeStorage docs](https://www.electronjs.org/docs/latest/api/safe-storage)

## Future Research

1. Investigate if IndexedDB contains cached conversation data
2. Extract protobuf definitions from ASAR source
3. Reverse engineer the encryption key derivation on Linux
4. Monitor network API for export endpoints
