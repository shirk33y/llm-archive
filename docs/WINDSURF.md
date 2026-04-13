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
- **Endpoint**: `http://localhost:<randomized_port>` (auto-detected)
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
```python
from llm_archive.ingestors.windsurf import LanguageServerClient

ls = LanguageServerClient()
cascade_ids = ls.get_all_cascade_ids()
trajectory = ls.get_trajectory(cascade_id)
```

### Protobuf Decoding

The `CortexTrajectoryStep` protobuf message contains:
- **Field 1**: `type` (enum - CortexStepType)
- **Field 4**: `status` (enum - ActionStatus)
- **Field 5**: `metadata` (message - CortexStepMetadata) - not returned by API
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

### Requirements
- Windsurf installed and running (for Language Server API)
- No external dependencies

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

1. **Field 5 (metadata) availability**: The Language Server API may or may not return the CortexStepMetadata field (field 5), which contains timestamps, model usage, costs, and execution details. The beautified extension code shows this field exists and contains a `created_at` timestamp (CortexStepMetadata field 1). Current implementation attempts to extract this timestamp if present.
2. **17.8% steps without data**: These are metadata-only steps (status updates, markers) that don't contain actual content.
3. **Language Server required**: Requires Windsurf to be running with the Language Server active.
4. **Protobuf reverse-engineering**: Decoders are based on reverse-engineering the protobuf structure from the beautified extension code. Field mappings may need updates if the schema changes.
5. **CSRF token**: Language Server API requires a valid CSRF token. The token is generated at Windsurf startup using `crypto.randomUUID()` and stored in the LanguageServerClient instance. It can be extracted via CDP by intercepting network requests, but CDP operations (especially deep object traversal) can cause Windsurf to hang or become unresponsive. The token is not stored in localStorage, sessionStorage, cookies, or easily accessible window properties.

## References

- Reddit discussions: r/Codeium and r/windsurf
- [ccl_chromium_reader](https://github.com/cclgroupltd/ccl_chromium_reader) - Chromium data extraction
- [getspecstory](https://github.com/specstoryai/getspecstory) - Third-party conversation export tool (Windsurf support pending)
- [Electron safeStorage docs](https://www.electronjs.org/docs/latest/api/safe-storage)
- `extension_beautified.js` - Beautified Windsurf extension code containing protobuf definitions

## Future Research

1. Investigate if Field 5 (metadata) can be obtained via alternative API endpoints
2. Monitor for protobuf schema changes in Windsurf updates
3. Add full-text search support for JSON data fields
4. Implement incremental sync to only process new/changed .pb files
