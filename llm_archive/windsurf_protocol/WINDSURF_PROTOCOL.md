# Windsurf Cascade Conversation Extraction

This document summarizes findings from reverse engineering Windsurf's conversation storage and the implemented solution for programmatic access.

## Storage Overview

Windsurf stores conversation data in multiple locations using different formats:

### 1. Encrypted Persistent Storage
- **Location**: `~/.codeium/windsurf/cascade/*.pb`
- **Format**: Encrypted Protocol Buffers (protobuf)
- **Status**: Not directly parseable without encryption key
- **Access**: Via Language Server API

### 2. Language Server API
- **Endpoint**: `http://a.localhost:<api_port>` (port auto-detected)
- **Method**: HTTP POST with protobuf-encoded requests
- **Status**: Fully implemented for historical .pb file access
- **Advantages**: Access to all historical conversations, no UI interaction required

### 3. LocalStorage Session Index
- **Key**: `cascade-open-sessions-by-workspace`
- **Format**: JSON
- **Content**: Workspace-scoped session metadata including tab IDs and active session

## Encryption Details

Windsurf uses Electron's `safeStorage` API for encrypting `.pb` files:

| Platform | Key Provider | Notes |
|----------|--------------|-------|
| macOS | Keychain Access | App-specific keys, user protection |
| Windows | DPAPI | User-scoped protection |
| Linux | Secret Service (GNOME/KWallet) or `basic_text` fallback | May use hardcoded password if no secret store |

**Key insight**: On Linux without a secret store, `safeStorage.getSelectedStorageBackend()` returns `basic_text`, indicating potential vulnerability.

## Language Server API Method

The Language Server API provides direct access to historical `.pb` files without requiring UI interaction.

### Connection

The language server listens on **two ports**, but only one serves the API. The other port silently hangs on requests. The correct port must be identified by probing.

```python
from llm_archive.ingestors.windsurf import LanguageServerClient

ls = LanguageServerClient()
trajectory = await ls.get_trajectory(cascade_id)
```

### Port Detection

The language server process (`language_server`) listens on 2 TCP ports:
- **API port**: Responds to HTTP requests (returns HTTP errors like 401/500 for invalid requests)
- **Silent port**: Accepts connections but never responds (hangs until timeout)

Detection strategy:
1. Find all LISTEN ports from the `language_server` process via `psutil`
2. Probe each port with a short timeout (2s) — the API port responds, the silent one hangs
3. Cache the working API port for the session

### Subdomain

The URL subdomain (e.g., `a.localhost`, `t.localhost`) **does not matter**. Any single lowercase letter works identically — all 26 subdomains route to the same API. The subdomain can be hardcoded to `a.localhost`.

Previous analysis showed the Windsurf UI cycling through different subdomains, leading to the assumption that the subdomain was significant. Empirical testing proved otherwise: all subdomains return identical responses with the same CSRF token.

### CSRF Token

The API requires a valid `x-codeium-csrf-token` header. Key behaviors:

- **Generated at LS startup** using `crypto.randomUUID()`
- **Invalidated on LS restart** — the old token returns HTTP 401 with `{"code":"unauthenticated","message":"invalid CSRF token"}`
- **Auto-refresh via CDP** — when a 401 is detected, the client automatically extracts a fresh token by intercepting network requests via CDP
- **Not in standard storage** — not in localStorage, sessionStorage, or cookies; only appears in HTTP request headers

### Request Flow

1. Discover API port via `_find_ls_ports()` + `_probe_api_port()`
2. Load CSRF token from `~/.llm-archive/auth/windsurf.json`
3. POST to `http://a.localhost:<port>/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory`
4. On HTTP 401 → auto-refresh CSRF token via CDP, retry once
5. On connection failure → re-probe ports (LS may have restarted), retry once
6. On success → decode protobuf response

### Required Headers

```
x-codeium-csrf-token: <uuid>
connect-protocol-version: 1
content-type: application/proto
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Windsurf/1.110.1 Chrome/142.0.7444.265 Electron/39.6.0 Safari/537.36
```

### Timeout Considerations

Large trajectories (9+ MB .pb files) can take 30-40 seconds for the LS to process and return. Use a 60-second timeout for `GetCascadeTrajectory` requests. Port probing uses 2-second timeouts since the API port responds quickly to invalid requests.

### Protobuf Decoding

The `CortexTrajectoryStep` protobuf message contains:
- **Field 1**: `type` (enum - CortexStepType)
- **Field 4**: `status` (enum - ActionStatus)
- **Field 5**: `metadata` (message - CortexStepMetadata) - contains `created_at` timestamp (verified: all steps return timestamps)
- **Fields 6-115**: Oneof step data fields

### Step Type Enum (CortexStepType)

| Value | Name | Description |
|-------|------|-------------|
| 0 | UNSPECIFIED | Unknown step type |
| 1 | DUMMY | Placeholder |
| 2 | FINISH | Step completion |
| 3 | PLAN_INPUT | Plan input |
| 4 | MQUERY | Model query |
| 5 | CODE_ACTION | Code action |
| 6 | GIT_COMMIT | Git commit |
| 7 | GREP_SEARCH | Grep search |
| 8 | VIEW_FILE | View file |
| 9 | LIST_DIRECTORY | List directory |
| 10 | COMPILE | Compile |
| 11 | INFORM | Inform |
| 12 | FILE_BREAKDOWN | File breakdown |
| 13 | VIEW_CODE_ITEM | View code item |
| 14 | USER_INPUT | User input |
| 15 | PLANNER_RESPONSE | Planner response |
| 16 | WRITE_TO_FILE | Write to file |
| 21 | RUN_COMMAND | Run command |
| 22 | RELATED_FILES | Related files |
| 23 | CHECKPOINT | Checkpoint |
| 24 | ERROR_MESSAGE | Error message |
| 25 | FIND | Find/search |
| 28 | COMMAND_STATUS | Command status |
| 29 | MEMORY | Memory |
| 31 | READ_URL_CONTENT | Read URL content |
| 32 | VIEW_CONTENT_CHUNK | View content chunk |
| 33 | SEARCH_WEB | Web search |
| 34 | RETRIEVE_MEMORY | Retrieve memory |
| 38 | MCP_TOOL | MCP tool |
| 51 | LIST_RESOURCES | List resources |
| 52 | READ_RESOURCE | Read resource |
| 65 | READ_TERMINAL | Read terminal |
| 73 | TODO_LIST | TODO list |
| 83 | EDIT_NOTEBOOK | Edit notebook |
| 87 | FIND_CODE_CONTEXT | Find code context |
| 91 | GREP_SEARCH_V2 | Grep search v2 |
| 100 | ASK_USER_QUESTION | Ask user question |

### Field Mappings

| Field Number | Oneof Field | Decoder | Description |
|--------------|-------------|---------|-------------|
| 6 | context_memory | `_decode_context_memory` | Binary protobuf with UUIDs, timestamps |
| 10 | file_content_edit | `_decode_file_content_edit` | Shell scripts, .desktop files |
| 19 | user_input | `_decode_user_input` | User text input |
| 20 | planner_response | `_decode_planner_response` | AI response text |
| 22 | write_file | `_decode_write_file` | File path and content |
| 23 | run_command | `_decode_run_command` | Command and stdout |
| 24 | error_message | `_decode_error_message` | Error text from nested protobuf |
| 25 | find | `_decode_find` | Find/search results |
| 28 | command_status | `_decode_command_status` | Command status updates |
| 29 | memory | `_decode_memory` | Memory data |
| 31 | read_url_content | `_decode_read_url_content` | URL content |
| 38 | project_context | `_decode_project_context` | Project context with UUIDs |
| 41 | parsed_url_content | `_decode_parsed_url_content` | Markdown with links |
| 42 | search_web | `_decode_search_web` | Web search query/results |
| 43 | empty_field | Generic | Empty string |
| 47 | mcp_tool | `_decode_mcp_tool` | MCP tool call data |
| 56 | url_reference | `_decode_url_reference` | URLs |
| 62 | tool_name | Generic | Tool name |
| 63 | tool_name_with_state | Generic | Tool name with state |
| 87 | plan | `_decode_plan` | Plan data |
| 101 | grep_result | Generic | Grep results |
| 105 | file_search_pattern | `_decode_file_search_pattern` | Search pattern |
| 109 | - | Generic | Rare field |
| 115 | ask_user_question | Generic | User question |

### Data Coverage

- **Total steps**: 3645
- **Decoded steps**: 2998 (82.2%)
- **Unknown fields**: 0
- **Steps without data**: 647 (17.8%) - metadata-only steps (status updates, markers)

The 17.8% of steps without data are normal - they use common fields (type, status) without oneof data fields. These are typically status updates, markers, or steps that don't contain actual content.

### Decoder Functions

#### Text-based decoders
- `_decode_user_input`: Extracts user query/response text
- `_decode_planner_response`: Extracts AI response text
- `_decode_error_message`: Extracts readable error text from nested protobuf using regex
- `_decode_file_content_edit`: Extracts shell scripts, .desktop files
- `_decode_parsed_url_content`: Extracts markdown with links
- `_decode_url_reference`: Extracts URLs

#### Structured decoders
- `_decode_write_file`: Extracts path and content
- `_decode_run_command`: Extracts command, stdout, exit_code
- `_decode_find`: Extracts pattern and results
- `_decode_command_status`: Extracts command status
- `_decode_memory`: Extracts memory data
- `_decode_read_url_content`: Extracts URL and content
- `_decode_search_web`: Extracts query and results
- `_decode_mcp_tool`: Extracts tool name and args
- `_decode_plan`: Extracts plan_id and plans
- `_decode_file_search_pattern`: Extracts pattern and results

#### Binary decoders
- `_decode_context_memory`: Extracts summary from binary protobuf (UUIDs, timestamps)
- `_decode_project_context`: Extracts summary from binary protobuf (task descriptions)

## Implementation

The `WindsurfIngestor` class in `llm_archive/ingestors/windsurf.py` implements Language Server API extraction:

```python
from llm_archive.ingestors import get_ingestor

# Use Language Server API (historical .pb files)
ingestor = get_ingestor("windsurf")
await ingestor.init()

# Extract all conversations
async for thread in ingestor.threads():
    print(f"{thread.title}: {len(thread.messages)} messages")
```

### Features

- **Language Server API**: Access to all historical .pb files
- **Full protobuf decoding**: 100% field coverage for step data
- **No truncation**: Stores full data without size limits
- **Force sync**: `-f` flag bypasses SHA1 deduplication to re-ingest
- **Smart sync**: Skips conversations whose `.pb` files haven't changed since last sync (compares mtime against DB `updated_at`)
- **Progress reporting**: Shows conversation title in progress bar during fetch
- **Auto CSRF refresh**: Detects stale tokens and refreshes via CDP automatically
- **Auto port detection**: Probes for the correct API port among the LS's two listening ports

### Requirements

- Windsurf installed and running (for Language Server API)
- Windsurf started with `--remote-debugging-port=9222` (for initial CSRF token extraction; auto-refreshed on 401)
- `psutil` Python package (for port detection)

### CLI Usage

```bash
# Sync using Language Server API (default)
uv run llm-archive sync windsurf

# Force re-ingestion (bypass SHA1 deduplication)
uv run llm-archive sync windsurf -f

# Custom database path
uv run llm-archive sync windsurf --db-path ~/my-archive.db
```

### Message Mapping

| Step Type | Role | Parts | Data Field |
|-----------|------|-------|------------|
| 14 (USER_INPUT) | `user` | `text` | `user_input` |
| 15 (PLANNER_RESPONSE) | `assistant` | `text` | `planner_response` |
| 21 (RUN_COMMAND) | `tool` | `tool_call` | `run_command` |
| 23 (WRITE_TO_FILE) | `tool` | `tool_call` | `write_file` |
| 8 (VIEW_FILE) | `tool` | `tool_call` | `view_file` |
| 9 (LIST_DIRECTORY) | `tool` | `tool_call` | `list_directory` |
| 7 (GREP_SEARCH) | `tool` | `tool_call` | `grep_search` |
| 33 (SEARCH_WEB) | `tool` | `tool_call` | `search_web` |
| 23 (CHECKPOINT) | `tool` | `tool_call` | `checkpoint` |
| 24 (ERROR_MESSAGE) | `tool` | `tool_call` | `error_message` |
| 25 (FIND) | `tool` | `tool_call` | `find` |
| 28 (COMMAND_STATUS) | `tool` | `tool_call` | `command_status` |
| 29 (MEMORY) | `tool` | `tool_call` | `memory` |
| 31 (READ_URL_CONTENT) | `tool` | `tool_call` | `read_url_content` |
| 38 (MCP_TOOL) | `tool` | `tool_call` | `mcp_tool` |
| 87 (PLAN) | `tool` | `tool_call` | `plan` |
| 115 (ASK_USER_QUESTION) | `tool` | `tool_call` | `ask_user_question` |
| 10 (file_content_edit) | `tool` | `tool_call` | `file_content_edit` |
| 41 (parsed_url_content) | `tool` | `tool_call` | `parsed_url_content` |
| 105 (file_search_pattern) | `tool` | `tool_call` | `file_search_pattern` |
| 6 (context_memory) | `tool` | `tool_call` | `context_memory` |
| 38 (project_context) | `tool` | `tool_call` | `project_context` |
| 56 (url_reference) | `tool` | `tool_call` | `url_reference` |

### Database Schema

Data is stored in SQLite with the following structure:

- **threads**: Thread metadata (id, title, source_id)
- **messages**: Message metadata (id, thread_id, role, content, metadata)
- **message_parts**: Message parts (id, message_id, kind, text, search_text, data)
- **message_raw**: Raw protobuf data (message_id, raw)

The `data` field in `message_parts` stores JSON-structured data from decoded protobuf fields. This is queryable via SQLite JSON functions but not in the full-text search index.

## Known Limitations

1. **trajectory_id ≠ cascade_id**: The `trajectory_id` returned by the API differs from the `.pb` filename (cascade_id). This means thread IDs in the DB (`windsurf:ls:{trajectory_id}`) cannot be matched back to `.pb` files by name. Smart sync works around this by comparing `.pb` mtime against the newest `updated_at` in the DB as a proxy for last sync time.
2. **17.8% steps without data**: These are metadata-only steps (status updates, markers) that don't contain actual content.
3. **Language Server required**: Requires Windsurf to be running with the Language Server active.
4. **Protobuf reverse-engineering**: Decoders are based on reverse-engineering the protobuf structure from the beautified extension code. Field mappings may need updates if the schema changes.
5. **CSRF token expiry**: The CSRF token is invalidated when the language server restarts. The client auto-refreshes via CDP, but CDP must be enabled (`--remote-debugging-port=9222`).
6. **Large trajectories**: Very large conversations (9+ MB .pb) can take 30-40 seconds for the LS to process.

## Chrome DevTools Protocol (CDP)

CDP is used to extract fresh CSRF tokens when the cached one becomes stale (e.g., after LS restart).

### Connecting to Windsurf via CDP

Windsurf must be started with remote debugging enabled:

```bash
windsurf --remote-debugging-port=9222
```

Once running, connect to CDP via HTTP to list targets:

```bash
curl http://127.0.0.1:9222/json
```

This returns a JSON list of targets including the main page target with a WebSocket URL.

### CSRF Token Extraction via Network Interception

The Network domain intercepts HTTP requests to extract the CSRF token:

1. Connect to CDP WebSocket
2. Enable Network domain
3. Listen for `Network.requestWillBeSent` events
4. Filter for language server requests (URL contains `language_server`)
5. Extract `x-codeium-csrf-token` header

**Automatic extraction**: Windsurf makes automatic language server requests on startup (e.g., `GetUnleashData`). By waiting for these automatic requests, the CSRF token can be extracted without requiring user interaction. This method is safe and does not cause Windsurf to hang.

### CSRF Token Location

From beautified extension code analysis:
- Generated at startup with `crypto.randomUUID()`
- Stored in `H.LanguageServerClient.getInstance().csrfToken`
- Also set via `I.windsurfLanguageServer.setCsrfToken(token)`
- Not accessible via standard browser storage APIs
- Only appears in HTTP request headers to the language server

### Example Scripts

See `llm_archive/windsurf_protocol/` for CDP extraction scripts:
- `extract_csrf_safe.py` - Intercepts automatic language server requests (no user interaction, safe)
- `extract_csrf_from_request.py` - Intercepts network requests (requires user interaction)
- `extract_csrf_auto.py` - Searches window object (may cause hangs)

### CDP Safety Notes

- Deep object traversal via Runtime.evaluate can freeze Windsurf
- Network interception for automatic requests is safe and doesn't require user interaction
- CDP operations should be kept minimal to avoid stability issues
- Always test CDP scripts on a non-production Windsurf instance

## References

- Reddit discussions: r/Codeium and r/windsurf
- [ccl_chromium_reader](https://github.com/cclgroupltd/ccl_chromium_reader) - Chromium data extraction
- [getspecstory](https://github.com/specstoryai/getspecstory) - Third-party conversation export tool (Windsurf support pending)
- [Electron safeStorage docs](https://www.electronjs.org/docs/latest/api/safe-storage)
- `extension_beautified.js` - Beautified Windsurf extension code containing protobuf definitions

## Future Research

1. Monitor for protobuf schema changes in Windsurf updates
2. Add full-text search support for JSON data fields
3. Investigate if trajectory_id can be matched to cascade_id for per-thread smart sync
