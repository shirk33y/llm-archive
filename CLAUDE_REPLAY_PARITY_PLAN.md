# Claude Replay Parity Plan

Goal: Bring llm-archive tool call parsing to parity with claude-replay's structured format.

## Why This Matters

claude-replay preserves full tool call structure including:
- `tool_use_id`: Links tool_use to tool_result
- `input`: Full tool arguments (not truncated)
- `result`: Full tool output (not truncated)
- `is_error`: Boolean error status
- `resultTimestamp`: Separate timestamp for results

llm-archive currently flattens everything to text with 500-char truncation, losing:
- File edit diffs (apply_patch patches)
- Error status (exit codes)
- Tool call linking
- Command normalization (workdir, wrapper stripping)

---

## Stage 0: Test Migration (Foundation)

### 0.1 Copy claude-replay Test Fixtures
**File**: `tests/fixtures/claude-replay/`

Copy all fixtures from claude-replay/test/:

```bash
mkdir -p tests/fixtures/claude-replay
cp /path/to/claude-replay/test/fixture-*.jsonl tests/fixtures/claude-replay/
cp /path/to/claude-replay/test/fixture-*.json tests/fixtures/claude-replay/
```

Fixtures to copy:
- `fixture.jsonl` - Claude Code baseline
- `fixture-system-tags.jsonl` - System tag stripping
- `fixture-cursor.jsonl` - Cursor format
- `fixture-codex.jsonl` - Codex legacy format
- `fixture-codex-patch.jsonl` - Codex patch parsing
- `fixture-codex-edges.jsonl` - Codex edge cases
- `fixture-gemini.json` - Gemini CLI format
- `fixture-opencode.jsonl` - OpenCode format

### 0.2 Port Parser Tests to Python
**File**: `tests/test_parsers_parity.py`

Port test-parser.mjs assertions to pytest:

```python
import pytest
from pathlib import Path
from llm_archive.ingestors.claudecode import _parse_jsonl

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "claude-replay"

def test_claudecode_turn_parsing():
    """Parses turns from JSONL (port of test-parser.mjs:24)"""
    thread = _parse_jsonl(FIXTURE_DIR / "fixture.jsonl")
    assert thread is not None
    assert len(thread.messages) >= 3

def test_claudecode_user_text_extraction():
    """Extracts user text (port of test-parser.mjs:29)"""
    thread = _parse_jsonl(FIXTURE_DIR / "fixture.jsonl")
    user_msgs = [m for m in thread.messages if m.role == "user"]
    assert "Hello" in user_msgs[0].content or "2+2" in user_msgs[0].content

def test_claudecode_thinking_blocks():
    """Extracts thinking blocks (port of test-parser.mjs:45)"""
    thread = _parse_jsonl(FIXTURE_DIR / "fixture.jsonl")
    for msg in thread.messages:
        for part in msg.parts:
            if part.kind == "thinking":
                assert "thinking" in part.text.lower() or part.text

def test_claudecode_tool_calls_with_results():
    """Extracts tool calls with results (port of test-parser.mjs:59)"""
    thread = _parse_jsonl(FIXTURE_DIR / "fixture.jsonl")
    tool_parts = [p for m in thread.messages for p in m.parts if p.kind == "tool_use"]
    assert len(tool_parts) > 0
    tc = tool_parts[0].tool_call
    assert tc is not None
    assert tc.name
    assert tc.result is not None

def test_claudecode_timestamps():
    """Preserves timestamps (port of test-parser.mjs:75)"""
    thread = _parse_jsonl(FIXTURE_DIR / "fixture.jsonl")
    assert thread.created_at is not None or any(m.created_at for m in thread.messages)

def test_claudecode_metadata_entries():
    """Detects sessions with leading metadata entries (port of test-parser.mjs:80)"""
    # Create test JSONL with metadata
    test_jsonl = FIXTURE_DIR / "test-metadata.jsonl"
    test_jsonl.write_text("""{"type":"queue-operation","operation":"enqueue"}
{"type":"session-id","id":"abc-123"}
{"type":"user","message":{"role":"user","content":"Hello"},"timestamp":"2025-06-01T10:00:00Z"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Hi!"}]},"timestamp":"2025-06-01T10:00:01Z"}""")
    
    thread = _parse_jsonl(test_jsonl)
    assert thread is not None
    user_msgs = [m for m in thread.messages if m.role == "user"]
    assert "Hello" in user_msgs[0].content
    test_jsonl.unlink()

def test_codex_patch_parsing():
    """Parses Codex apply_patch format (port of test-parser.mjs:...)"""
    # TODO: Add after implementing Codex patch parsing
    pass

def test_codex_command_normalization():
    """Normalizes bash commands (port of test-parser.mjs:...)"""
    # TODO: Add after implementing Codex command normalization
    pass

def test_opencode_tool_mapping():
    """Maps tool names (port of test-parser.mjs:...)"""
    # TODO: Add after implementing OpenCode tool mapping
    pass

def test_opencode_error_detection():
    """Detects tool errors (port of test-parser.mjs:...)"""
    # TODO: Add after implementing OpenCode error detection
    pass
```

### 0.3 Run Baseline Tests
```bash
pytest tests/test_parsers_parity.py -v
```

Expected: Some tests fail (tool_call is None, truncated results)

---

## Stage 1: Schema Extensions

### 1.1 Extend IngestedPart Schema
**File**: `llm_archive/schema.py`

Add structured tool call support to `IngestedPart`:

```python
@dataclass
class ToolCall:
    tool_use_id: str = ""
    name: str = ""
    input: dict | None = None
    result: str | None = None
    resultTimestamp: int | None = None
    is_error: bool = False

@dataclass
class IngestedPart:
    kind: str
    text: str = ""
    data: dict = field(default_factory=dict)
    visible: bool = True
    searchable: bool = True
    tool_call: ToolCall | None = None  # NEW: structured tool data
```

### 1.2 Update Database Schema
**File**: `llm_archive/db.py`

Add tool call columns to `message_parts`:

```sql
ALTER TABLE message_parts ADD COLUMN tool_use_id TEXT;
ALTER TABLE message_parts ADD COLUMN tool_name TEXT;
ALTER TABLE message_parts ADD COLUMN tool_input TEXT;  -- JSON
ALTER TABLE message_parts ADD COLUMN tool_result TEXT;
ALTER TABLE message_parts ADD COLUMN tool_result_timestamp INTEGER;
ALTER TABLE message_parts ADD COLUMN tool_is_error INTEGER DEFAULT 0;
```

Update `save_thread()` to populate new columns.

---

## Stage 2: Claude Code Ingestor

### 2.1 Parse Structured Tool Calls
**File**: `llm_archive/ingestors/claudecode.py`

Extract full tool call structure from `tool_use` blocks:

```python
def _extract_tool_call(block: dict) -> ToolCall | None:
    if block.get("type") != "tool_use":
        return None
    return ToolCall(
        tool_use_id=block.get("id", ""),
        name=block.get("name", ""),
        input=block.get("input", {}),
        result=None,  # Filled later from tool_result
        resultTimestamp=None,
        is_error=False,
    )

def _extract_tool_result(block: dict) -> tuple[str, str | None, int | None, bool]:
    if block.get("type") != "tool_result":
        return None, None, None, False
    tid = block.get("tool_use_id", "")
    content = block.get("content")
    # Extract text from content array or string
    result_text = _flatten_content(content)
    error = block.get("is_error", False)
    return tid, result_text, block.get("timestamp"), error
```

### 2.2 Link tool_use to tool_result
**File**: `llm_archive/ingestors/claudecode.py`

Maintain pending tool calls and attach results:

```python
def _parse_jsonl(path: Path, index_meta: dict | None = None) -> IngestedThread | None:
    # ... existing code ...
    
    pending_tool_calls: dict[str, ToolCall] = {}
    messages: list[IngestedMessage] = []
    
    for entry in lines:
        # ... existing role/type checks ...
        
        content_blocks = msg_data.get("content", [])
        parts: list[IngestedPart] = []
        
        for block in content_blocks:
            if block.get("type") == "text":
                parts.append(IngestedPart(kind="text", text=block.get("text", "")))
            elif block.get("type") == "thinking":
                parts.append(IngestedPart(kind="thinking", text=block.get("thinking", "")))
            elif block.get("type") == "tool_use":
                tool_call = _extract_tool_call(block)
                if tool_call:
                    pending_tool_calls[tool_call.tool_use_id] = tool_call
                    parts.append(IngestedPart(
                        kind="tool_use",
                        text="",
                        tool_call=tool_call,
                    ))
            elif block.get("type") == "tool_result":
                tid, result, ts, error = _extract_tool_result(block)
                if tid and tid in pending_tool_calls:
                    tc = pending_tool_calls[tid]
                    tc.result = result
                    tc.resultTimestamp = _parse_timestamp(ts)
                    tc.is_error = error
                    # Add as separate part for tool_result
                    parts.append(IngestedPart(
                        kind="tool_result",
                        text=result or "",
                        tool_call=tc,
                    ))
        
        messages.append(IngestedMessage(
            id=f"claudecode:{msg_id}",
            thread_id=thread_id,
            role=role,
            content=_flatten_content(content_blocks),
            parts=parts,  # NEW: preserve parts
            created_at=ts,
            metadata=metadata,
        ))
```

### 2.3 Remove 500-char Truncation
**File**: `llm_archive/ingestors/claudecode.py`

Remove `[:500]` truncation from `_flatten_content()` for tool results.

### 2.4 Verify with Tests
```bash
pytest tests/test_parsers_parity.py::test_claudecode_tool_calls_with_results -v
```

Expected: Passes

---

## Stage 3: Codex Ingestor

### 3.1 Add New Format Support
**File**: `llm_archive/ingestors/codex.py`

Parse new `thread.started` / `item.completed` format:

```python
def _parse_new_format(events: list[dict]) -> IngestedThread | None:
    blocks: list[IngestedPart] = []
    user_text = ""
    timestamp: int | None = None
    
    for evt in events:
        if evt.get("type") != "item.completed":
            continue
        
        item = evt.get("item", {})
        item_type = item.get("type", "")
        ts = _parse_ts(evt.get("timestamp"))
        if ts:
            timestamp = ts
        
        if item_type == "command_execution":
            cmd = item.get("command", "")
            clean_cmd = _normalize_codex_command(cmd)
            tool_call = ToolCall(
                tool_use_id=item.get("id", ""),
                name="Bash",
                input={"command": clean_cmd},
                result=item.get("aggregated_output", "").strip(),
                resultTimestamp=ts,
                is_error=item.get("exit_code", 0) != 0,
            )
            blocks.append(IngestedPart(
                kind="tool_use",
                text="",
                tool_call=tool_call,
            ))
        elif item_type == "function_call":
            name = item.get("name", "")
            args = item.get("arguments", "")
            tool_call = _parse_codex_function_call(name, args, item)
            if tool_call:
                blocks.append(IngestedPart(
                    kind="tool_use",
                    text="",
                    tool_call=tool_call,
                ))
        elif item_type == "agent_message":
            blocks.append(IngestedPart(
                kind="text",
                text=item.get("text", ""),
            ))
    
    if not blocks:
        return None
    
    return IngestedThread(
        id=f"codex:{session_id}",
        source_id="codex",
        title=user_text or "Codex Session",
        created_at=timestamp,
        updated_at=timestamp,
        messages=[IngestedMessage(
            id=f"codex:{session_id}:0",
            thread_id=f"codex:{session_id}",
            role="assistant",
            content=user_text,
            parts=blocks,
            created_at=timestamp,
        )],
    )

def _normalize_codex_command(cmd: str) -> str:
    """Strip /bin/bash -lc wrapper."""
    cmd = re.sub(r"^/bin/bash\s+-lc\s+", "", cmd)
    cmd = cmd.strip().strip("'").strip('"')
    return cmd

def _parse_codex_function_call(name: str, args: str, item: dict) -> ToolCall | None:
    """Parse Codex function_call with tool name mapping."""
    mapped_name = name
    input_data = {}
    
    if name == "exec_command":
        mapped_name = "Bash"
        try:
            parsed = json.loads(args)
            cmd = parsed.get("cmd", "")
            workdir = parsed.get("workdir")
            if workdir:
                cmd = f"cd {workdir} && {cmd}"
            input_data = {"command": cmd}
        except json.JSONDecodeError:
            input_data = {"command": args}
    elif name == "apply_patch":
        mapped_name = "Edit" if not args.startswith("*** Add File:") else "Write"
        input_data = _parse_codex_patch(args)
    else:
        try:
            input_data = json.loads(args)
        except json.JSONDecodeError:
            input_data = {"raw": args}
    
    return ToolCall(
        tool_use_id=item.get("id", ""),
        name=mapped_name,
        input=input_data,
        result=item.get("output", "").strip(),
        resultTimestamp=_parse_ts(item.get("timestamp")),
        is_error=item.get("status") == "failed",
    )

def _parse_codex_patch(patch_str: str) -> dict:
    """Parse Codex apply_patch format into Edit/Write input."""
    lines = patch_str.split("\n")
    file_path = ""
    old_lines = []
    new_lines = []
    is_new = False
    
    for line in lines:
        if line.startswith("*** Add File:"):
            file_path = line.replace("*** Add File:", "").strip()
            is_new = True
        elif line.startswith("*** Update File:"):
            file_path = line.replace("*** Update File:", "").strip()
            is_new = False
        elif line.startswith("@@") or line.startswith("***"):
            continue
        elif line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith("-"):
            old_lines.append(line[1:])
        else:
            old_lines.append(line)
            new_lines.append(line)
    
    if is_new:
        return {"file_path": file_path, "content": "\n".join(new_lines)}
    return {
        "file_path": file_path,
        "old_string": "\n".join(old_lines),
        "new_string": "\n".join(new_lines),
    }
```

### 3.2 Detect Format Version
**File**: `llm_archive/ingestors/codex.py`

Add format detection to `_parse_session()`:

```python
def _parse_session(path: Path, thread_id: str, meta: dict) -> IngestedThread | None:
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    
    # Detect new format
    is_new_format = any(e.get("type") in ("thread.started", "item.completed") for e in events)
    if is_new_format:
        return _parse_new_format(events)
    
    # Continue with legacy format parsing
    # ... existing code ...
```

### 3.3 Remove Truncation
**File**: `llm_archive/ingestors/codex.py`

Remove all `[:500]` truncations.

### 3.4 Verify with Tests
```bash
pytest tests/test_parsers_parity.py::test_codex_patch_parsing -v
pytest tests/test_parsers_parity.py::test_codex_command_normalization -v
```

Expected: Passes

---

## Stage 4: OpenCode Ingestor

### 4.1 Add Tool Name Mapping
**File**: `llm_archive/ingestors/opencode.py`

Add tool name normalization:

```python
TOOL_MAP = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "ls": "Glob",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "codesearch": "Grep",
    "task": "Task",
    "todo": "TodoWrite",
}

def _map_tool_name(name: str) -> str:
    return TOOL_MAP.get(name, name)
```

### 4.2 Parse Tool Calls with Error Detection
**File**: `llm_archive/ingestors/opencode.py`

Extract full tool call structure from SQLite rows:

```python
def _parse_tool_use_event(row: dict) -> IngestedPart | None:
    state = row.get("state", {})
    input_data = state.get("input", {})
    output = state.get("output", "")
    tool_name = state.get("tool", "unknown")
    mapped_name = _map_tool_name(tool_name)
    
    # Normalize bash workdir
    if mapped_name == "Bash" and "command" in input_data:
        workdir = input_data.get("workdir")
        command = input_data.get("command", "")
        if workdir:
            input_data = {"command": f"cd {workdir} && {command}"}
    
    # Detect error
    is_error = (
        state.get("status") == "error" or
        state.get("metadata", {}).get("exit", 0) != 0
    )
    
    # Get result timestamp
    result_ts = state.get("time", {}).get("end")
    if result_ts:
        result_ts = _parse_timestamp(result_ts)
    
    tool_call = ToolCall(
        tool_use_id=row.get("callID", ""),
        name=mapped_name,
        input=input_data,
        result=str(output) if output else None,
        resultTimestamp=result_ts,
        is_error=is_error,
    )
    
    return IngestedPart(
        kind="tool_use",
        text="",
        tool_call=tool_call,
    )
```

### 4.3 Preserve Parts in Messages
**File**: `llm_archive/ingestors/opencode.py`

Collect parts for each message instead of flattening:

```python
# In _parse_session():
parts: list[IngestedPart] = []
for row in rows:
    etype = row.get("type")
    if etype == "tool_use":
        part = _parse_tool_use_event(row)
        if part:
            parts.append(part)
    elif etype == "reasoning":
        text = row.get("part", {}).get("text", "")
        if text.strip():
            parts.append(IngestedPart(kind="thinking", text=text))
    elif etype == "text":
        text = row.get("part", {}).get("text", "")
        if text.strip():
            parts.append(IngestedPart(kind="text", text=text))
```

### 4.4 Verify with Tests
```bash
pytest tests/test_parsers_parity.py::test_opencode_tool_mapping -v
pytest tests/test_parsers_parity.py::test_opencode_error_detection -v
```

Expected: Passes

---

## Stage 5: Database Migration

### 5.1 Create Migration Script
**File**: `scripts/migrate_tool_calls.py`

Add migration to add new columns:

```python
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".llm-archive" / "archive.db"

def migrate():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    
    # Check if columns already exist
    cur.execute("PRAGMA table_info(message_parts)")
    columns = {row[1] for row in cur.fetchall()}
    
    new_columns = [
        ("tool_use_id", "TEXT"),
        ("tool_name", "TEXT"),
        ("tool_input", "TEXT"),
        ("tool_result", "TEXT"),
        ("tool_result_timestamp", "INTEGER"),
        ("tool_is_error", "INTEGER DEFAULT 0"),
    ]
    
    for col_name, col_type in new_columns:
        if col_name not in columns:
            cur.execute(f"ALTER TABLE message_parts ADD COLUMN {col_name} {col_type}")
            print(f"Added column: {col_name}")
    
    con.commit()
    con.close()
    print("Migration complete")

if __name__ == "__main__":
    migrate()
```

### 5.2 Run Migration
```bash
python scripts/migrate_tool_calls.py
```

---

## Stage 6: Cursor Ingestor (New)

### 6.1 Create Cursor Ingestor
**File**: `llm_archive/ingestors/cursor.py`

Cursor format is identical to Claude Code (same JSONL structure). Reuse shared parsing:

```python
from __future__ import annotations
import json
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.ingestors.claudecode import _parse_timestamp, _flatten_content, _extract_tool_call, _extract_tool_result, ToolCall, IngestedPart
from llm_archive.schema import IngestedMessage, IngestedThread

DEFAULT_ROOT = Path.home() / ".cursor" / "projects"


def _parse_jsonl(path: Path) -> IngestedThread | None:
    """Parse Cursor JSONL - same format as Claude Code, no timestamps."""
    lines = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        return None

    if not lines:
        return None

    thread_id = f"cursor:{path.stem}"
    messages: list[IngestedMessage] = []
    pending_tool_calls: dict[str, ToolCall] = {}

    for entry in lines:
        role = entry.get("role")
        if role not in ("user", "assistant"):
            continue

        content_blocks = entry.get("message", {}).get("content", [])
        parts: list[IngestedPart] = []

        for block in content_blocks:
            if block.get("type") == "text":
                parts.append(IngestedPart(kind="text", text=block.get("text", "")))
            elif block.get("type") == "tool_use":
                tool_call = _extract_tool_call(block)
                if tool_call:
                    pending_tool_calls[tool_call.tool_use_id] = tool_call
                    parts.append(IngestedPart(
                        kind="tool_use",
                        text="",
                        tool_call=tool_call,
                    ))
            elif block.get("type") == "tool_result":
                tid, result, _, error = _extract_tool_result(block)
                if tid and tid in pending_tool_calls:
                    tc = pending_tool_calls[tid]
                    tc.result = result
                    tc.is_error = error
                    parts.append(IngestedPart(
                        kind="tool_result",
                        text=result or "",
                        tool_call=tc,
                    ))

        messages.append(IngestedMessage(
            id=f"cursor:{thread_id}:{len(messages)}",
            thread_id=thread_id,
            role=role,
            content=_flatten_content(content_blocks),
            parts=parts,
            created_at=None,  # Cursor has no timestamps
            metadata={},
        ))

    if not messages:
        return None

    first_user = next((m for m in messages if m.role == "user"), None)
    title = first_user.content[:80].split("\n")[0].strip() if first_user else "Cursor Session"

    return IngestedThread(
        id=thread_id,
        source_id="cursor",
        title=title,
        created_at=None,
        updated_at=None,
        messages=messages,
    )


class CursorIngestor(BaseIngestor):
    source_id = "cursor"

    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = root

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        pass

    async def count_threads(self, since: int | None = None) -> int:
        if not self.root.exists():
            return 0
        count = 0
        for project_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            transcript_dir = project_dir / "agent-transcripts"
            if not transcript_dir.exists():
                continue
            for jsonl_path in sorted(transcript_dir.rglob("*.jsonl")):
                count += 1
        return count

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        if not self.root.exists():
            return
        for project_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            transcript_dir = project_dir / "agent-transcripts"
            if not transcript_dir.exists():
                continue
            for jsonl_path in sorted(transcript_dir.rglob("*.jsonl")):
                thread = _parse_jsonl(jsonl_path)
                if thread:
                    yield thread
```

### 6.2 Register Cursor Ingestor
**File**: `llm_archive/ingestors/__init__.py`

```python
from llm_archive.ingestors.cursor import CursorIngestor

INGESTORS = {
    "claudecode": ClaudeCodeIngestor,
    "cursor": CursorIngestor,
    "codex": CodexIngestor,
    # ...
}
```

### 6.3 Verify with Tests
```bash
llm-archive sync cursor
llm-archive list sources
```

Expected: Cursor source appears in list

---

## Stage 7: Gemini CLI Ingestor (New)

### 7.1 Create Gemini Ingestor
**File**: `llm_archive/ingestors/gemini.py`

Gemini format is single JSON with nested messages:

```python
from __future__ import annotations
import json
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedThread, IngestedPart, ToolCall

DEFAULT_ROOT = Path.home() / ".gemini" / "tmp"

TOOL_MAP = {
    "run_shell_command": "Bash",
    "read_file": "Read",
    "edit_file": "Edit",
    "write_file": "Write",
    "search_files": "Grep",
    "list_directory": "Glob",
}


def _parse_timestamp(ts: str) -> int | None:
    if not ts:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _parse_json(path: Path) -> IngestedThread | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    session_id = data.get("sessionId", path.stem)
    thread_id = f"gemini:{session_id}"
    messages: list[IngestedMessage] = []
    pending_tool_calls: dict[str, ToolCall] = {}

    for msg_data in data.get("messages", []):
        msg_type = msg_data.get("type")  # "user" or "gemini"
        if msg_type not in ("user", "gemini"):
            continue

        role = "assistant" if msg_type == "gemini" else "user"
        content = msg_data.get("content", "")
        timestamp = _parse_timestamp(msg_data.get("timestamp"))
        msg_id = msg_data.get("id", f"{session_id}:{len(messages)}")

        parts: list[IngestedPart] = []

        if msg_type == "gemini":
            # Process thoughts (thinking blocks)
            for thought in msg_data.get("thoughts", []):
                text = thought.get("description", "")
                if text.strip():
                    parts.append(IngestedPart(kind="thinking", text=text))

            # Process tool calls
            for tool_call_data in msg_data.get("toolCalls", []):
                tid = tool_call_data.get("id", "")
                name = tool_call_data.get("name", "")
                mapped_name = TOOL_MAP.get(name, name)
                args = tool_call_data.get("args", {})

                # Extract result from nested functionResponse
                result = None
                result_ts = None
                is_error = False
                responses = tool_call_data.get("result", [])
                if responses:
                    for resp in responses:
                        func_resp = resp.get("functionResponse", {})
                        if func_resp.get("id") == tid:
                            response_data = func_resp.get("response", {})
                            result = response_data.get("output", "")
                            is_error = response_data.get("exitCode", 0) != 0
                            break

                tc = ToolCall(
                    tool_use_id=tid,
                    name=mapped_name,
                    input=args,
                    result=result,
                    resultTimestamp=result_ts,
                    is_error=is_error,
                )
                pending_tool_calls[tid] = tc
                parts.append(IngestedPart(
                    kind="tool_use",
                    text="",
                    tool_call=tc,
                ))

        # Add content as text part
        if content.strip():
            parts.append(IngestedPart(kind="text", text=content))

        messages.append(IngestedMessage(
            id=f"gemini:{msg_id}",
            thread_id=thread_id,
            role=role,
            content=content,
            parts=parts,
            created_at=timestamp,
            metadata={},
        ))

    if not messages:
        return None

    first_user = next((m for m in messages if m.role == "user"), None)
    title = first_user.content[:80].split("\n")[0].strip() if first_user else "Gemini Session"

    created_at = _parse_timestamp(data.get("startTime"))
    updated_at = _parse_timestamp(data.get("lastUpdated"))

    return IngestedThread(
        id=thread_id,
        source_id="gemini",
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
    )


class GeminiIngestor(BaseIngestor):
    source_id = "gemini"

    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = root

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        pass

    async def count_threads(self, since: int | None = None) -> int:
        if not self.root.exists():
            return 0
        count = 0
        for project_hash in self.root.iterdir():
            if not project_hash.is_dir():
                continue
            chats_dir = project_hash / "chats"
            if not chats_dir.exists():
                continue
            for json_path in sorted(chats_dir.glob("*.json")):
                if since:
                    updated = json_path.stat().st_mtime * 1000
                    if updated < since:
                        continue
                count += 1
        return count

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        if not self.root.exists():
            return
        for project_hash in sorted(self.root.iterdir()):
            if not project_hash.is_dir():
                continue
            chats_dir = project_hash / "chats"
            if not chats_dir.exists():
                continue
            for json_path in sorted(chats_dir.glob("*.json")):
                thread = _parse_json(json_path)
                if thread:
                    yield thread
```

### 7.2 Register Gemini Ingestor
**File**: `llm_archive/ingestors/__init__.py`

```python
from llm_archive.ingestors.gemini import GeminiIngestor

INGESTORS = {
    "claudecode": ClaudeCodeIngestor,
    "cursor": CursorIngestor,
    "codex": CodexIngestor,
    "gemini": GeminiIngestor,
    # ...
}
```

### 7.3 Verify with Tests
```bash
llm-archive sync gemini
llm-archive list sources
```

Expected: Gemini source appears in list

---

## Stage 8: Search and Display

### 8.1 Update Search to Include Tool Data
**File**: `llm_archive/mcp_server.py`

Include tool call data in search results:

```python
# In get_message() and get_thread():
for part in msg.get("parts", []):
    if part.get("tool_call"):
        tc = part["tool_call"]
        # Add to search_text for better searchability
        search_text += f" {tc['name']}"
        if tc.get("input"):
            search_text += f" {json.dumps(tc['input'], ensure_ascii=False)}"
        if tc.get("result"):
            search_text += f" {tc['result']}"
```

### 8.2 Update CLI Display
**File**: `llm_archive/cli.py`

Display tool calls with error status:

```python
def _format_part(part: dict) -> str:
    kind = part.get("kind")
    if kind == "tool_use":
        tc = part.get("tool_call", {})
        name = tc.get("name", "Unknown")
        error = " [ERROR]" if tc.get("is_error") else ""
        return f"[Tool: {name}]{error}"
    elif kind == "thinking":
        return f"[Thinking]"
    return part.get("text", "")
```

---

## Stage 9: Testing

### 9.1 Run All Parity Tests
```bash
pytest tests/test_parsers_parity.py -v
```

Expected: All tests pass

### 9.2 Add Ingestion Tests
**File**: `tests/test_ingestion.py`

Test full ingestion for all sources:

```python
import pytest
from llm_archive.ingestors.claudecode import ClaudeCodeIngestor
from llm_archive.ingestors.cursor import CursorIngestor
from llm_archive.ingestors.codex import CodexIngestor
from llm_archive.ingestors.gemini import GeminiIngestor
from llm_archive.ingestors.opencode import OpenCodeIngestor

@pytest.mark.asyncio
async def test_claudecode_ingestion_with_tools():
    ingestor = ClaudeCodeIngestor()
    threads = [t async for t in ingestor.threads()]
    if not threads:
        pytest.skip("No Claude Code sessions found")
    for thread in threads:
        for msg in thread.messages:
            for part in msg.parts:
                if part.kind == "tool_use":
                    assert part.tool_call is not None
                    assert part.tool_call.name

@pytest.mark.asyncio
async def test_cursor_ingestion_with_tools():
    ingestor = CursorIngestor()
    threads = [t async for t in ingestor.threads()]
    if not threads:
        pytest.skip("No Cursor sessions found")
    for thread in threads:
        for msg in thread.messages:
            for part in msg.parts:
                if part.kind == "tool_use":
                    assert part.tool_call is not None

@pytest.mark.asyncio
async def test_codex_ingestion_with_tools():
    ingestor = CodexIngestor()
    threads = [t async for t in ingestor.threads()]
    if not threads:
        pytest.skip("No Codex sessions found")
    for thread in threads:
        for msg in thread.messages:
            for part in msg.parts:
                if part.kind == "tool_use":
                    assert part.tool_call is not None
                    if part.tool_call.name == "Bash":
                        assert "command" in part.tool_call.input

@pytest.mark.asyncio
async def test_gemini_ingestion_with_tools():
    ingestor = GeminiIngestor()
    threads = [t async for t in ingestor.threads()]
    if not threads:
        pytest.skip("No Gemini sessions found")
    for thread in threads:
        for msg in thread.messages:
            for part in msg.parts:
                if part.kind == "tool_use":
                    assert part.tool_call is not None

@pytest.mark.asyncio
async def test_opencode_ingestion_with_tools():
    ingestor = OpenCodeIngestor()
    threads = [t async for t in ingestor.threads()]
    if not threads:
        pytest.skip("No OpenCode sessions found")
    for thread in threads:
        for msg in thread.messages:
            for part in msg.parts:
                if part.kind == "tool_use":
                    assert part.tool_call is not None
                    assert part.tool_call.is_error is not None
```

### 9.3 Run Integration Tests
```bash
pytest tests/test_ingestion.py -v
```

Expected: All tests pass (or skip if no sessions)

---

## Stage 10: Documentation

### 10.1 Update Schema Docs
**File**: `docs/SCHEMA.md`

Document new tool call structure:

```markdown
### Tool Calls

Tool calls are stored with full structure in `IngestedPart.tool_call`:

```python
ToolCall(
    tool_use_id: str,  # Links tool_use to tool_result
    name: str,        # Normalized tool name
    input: dict,      # Full tool arguments
    result: str,      # Full tool output
    resultTimestamp: int,  # Milliseconds
    is_error: bool,   # Exit code or status error
)
```

### 10.2 Update Ingestor Docs
**File**: `docs/INGESTORS.md`

Document all ingestors:

```markdown
## Supported Sources

| Source | Format | Location | Auth |
|--------|--------|----------|------|
| Claude Code | JSONL | `~/.claude/projects/` | None |
| Cursor | JSONL | `~/.cursor/projects/agent-transcripts/` | None |
| Codex CLI | JSONL | `~/.codex/sessions/` | None |
| Gemini CLI | JSON | `~/.gemini/tmp/` | None |
| OpenCode | SQLite | `~/.opencode/sessions.db` | None |
| ChatGPT | API | Web | Browser |
| Claude | API | Web | Browser |
| DeepSeek | API | Web | Browser |
| Windsurf | Protobuf | `~/.windsurf/` | None |
```

---

## Stage 11: Backward Compatibility

### 11.1 Fallback to Text Format
**File**: `llm_archive/db.py`

Handle messages without structured parts:

```python
def _attach_parts(con: sqlite3.Connection, msg: dict) -> dict:
    parts = [
        dict(r)
        for r in con.execute(
            "SELECT * FROM message_parts WHERE message_id=? ORDER BY ord",
            (msg["id"],),
        ).fetchall()
    ]
    
    # Fallback: if no parts but tool_name exists, reconstruct
    if not parts and msg.get("role") == "assistant":
        # Parse content to extract tool calls
        parts = _parse_legacy_tool_calls(msg.get("content", ""))
    
    msg["parts"] = parts
    return msg
```

### 11.2 Legacy Message Support
**File**: `llm_archive/cli.py`

Display legacy messages without parts:

```python
def _display_message(msg: dict):
    parts = msg.get("parts", [])
    if not parts:
        # Legacy format: display flat content
        print(f"  {msg.get('content', '')[:200]}")
    else:
        for part in parts:
            print(f"  {_format_part(part)}")
```

---

## Stage 12: Performance Optimization

### 12.1 Index Tool Columns
**File**: `llm_archive/db.py`

Add indexes for tool call queries:

```sql
CREATE INDEX IF NOT EXISTS idx_tool_use_id ON message_parts(tool_use_id);
CREATE INDEX IF NOT EXISTS idx_tool_name ON message_parts(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_error ON message_parts(tool_is_error) WHERE tool_is_error = 1;
```

### 12.2 Batch Tool Call Inserts
**File**: `llm_archive/db.py`

Batch insert tool call data:

```python
def save_thread(con: sqlite3.Connection, thread: IngestedThread, force: bool = False) -> bool:
    # ... existing code ...
    
    # Batch insert tool call data
    tool_data = []
    for msg in thread.messages:
        for i, part in enumerate(msg.parts):
            if part.tool_call:
                tool_data.append((
                    msg.id, i,
                    part.tool_call.tool_use_id,
                    part.tool_call.name,
                    json.dumps(part.tool_call.input) if part.tool_call.input else None,
                    part.tool_call.result,
                    part.tool_call.resultTimestamp,
                    1 if part.tool_call.is_error else 0,
                ))
    
    if tool_data:
        con.executemany(
            "UPDATE message_parts SET tool_use_id=?, tool_name=?, tool_input=?, "
            "tool_result=?, tool_result_timestamp=?, tool_is_error=? "
            "WHERE message_id=? AND ord=?",
            tool_data,
        )
```

---

## Stage 13: Verification

### 13.1 Compare with claude-replay Output
**File**: `scripts/verify_parity.py`

Parse a session with both tools and compare:

```python
import json
from pathlib import Path

def verify_claudecode_session(session_path: Path):
    # Parse with llm-archive
    from llm_archive.ingestors.claudecode import _parse_jsonl
    thread_llm = _parse_jsonl(session_path)
    
    # Parse with claude-replay
    from claude_replay import parseTranscriptFromText
    turns_cr = parseTranscriptFromText(session_path.read_text())
    
    # Compare tool call counts
    llm_tools = sum(1 for msg in thread_llm.messages for p in msg.parts if p.kind == "tool_use")
    cr_tools = sum(1 for turn in turns_cr for b in turn.blocks if b.kind == "tool_use")
    
    assert llm_tools == cr_tools, f"Tool count mismatch: {llm_tools} vs {cr_tools}"
    
    # Compare tool names
    llm_names = {p.tool_call.name for msg in thread_llm.messages for p in msg.parts if p.kind == "tool_use"}
    cr_names = {b.tool_call.name for turn in turns_cr for b in turn.blocks if b.kind == "tool_use"}
    
    assert llm_names == cr_names, f"Tool names mismatch: {llm_names} vs {cr_names}"
    
    print("✅ Tool call parity verified")

if __name__ == "__main__":
    verify_claudecode_session(Path.home() / ".claude/projects/-*/session-*.jsonl")
```

### 13.2 Run Verification
```bash
python scripts/verify_parity.py
```

---

## Stage 14: Release Notes

### 14.1 Update CHANGELOG
**File**: `CHANGELOG.md`

```markdown
## [Unreleased]

### Added
- Structured tool call storage with tool_use_id linking
- Tool name normalization across platforms (bash→Bash, etc.)
- Patch parsing for Codex apply_patch
- Error status tracking (exit codes, tool failures)
- Result timestamps separate from call timestamps
- Tool call search in FTS index
- **NEW**: Cursor ingestor support
- **NEW**: Gemini CLI ingestor support

### Fixed
- Removed 500-char truncation on tool inputs/outputs
- Added support for new Codex format (thread.started/item.completed)
- Fixed bash workdir normalization in OpenCode

### Changed
- IngestedPart now includes optional tool_call field
- message_parts table extended with 6 new columns
- Database migration required on upgrade
```

---

## Implementation Order

1. **Stage 0**: Test migration (foundation, correctness) - 4h P0
2. **Stage 1**: Schema extensions (foundation) - 4h P0
3. **Stage 2**: Claude Code ingestor (easiest, most complete) - 6h P0
4. **Stage 5**: Database migration (enable storage) - 2h P0
5. **Stage 3**: Codex ingestor (medium complexity) - 8h P0
6. **Stage 4**: OpenCode ingestor (medium complexity) - 4h P0
7. **Stage 6**: Cursor ingestor (new source) - 6h P1
8. **Stage 7**: Gemini ingestor (new source) - 8h P1
9. **Stage 8**: Search and display (user-facing) - 4h P1
10. **Stage 9**: Testing (validation) - 6h P1
11. **Stage 10**: Documentation (knowledge transfer) - 2h P2
12. **Stage 11**: Backward compatibility (safety) - 4h P1
13. **Stage 12**: Performance optimization (nice-to-have) - 4h P2
14. **Stage 13**: Verification (confidence) - 2h P1
15. **Stage 14**: Release (shipping) - 1h P2

---

## Estimated Effort

| Stage | Effort | Priority |
|-------|--------|----------|
| Stage 0: Test Migration | 4h | P0 |
| Stage 1: Schema | 4h | P0 |
| Stage 2: Claude Code | 6h | P0 |
| Stage 3: Codex | 8h | P0 |
| Stage 4: OpenCode | 4h | P0 |
| Stage 5: Migration | 2h | P0 |
| Stage 6: Cursor (New) | 6h | P1 |
| Stage 7: Gemini (New) | 8h | P1 |
| Stage 8: Search/Display | 4h | P1 |
| Stage 9: Testing | 6h | P1 |
| Stage 10: Documentation | 2h | P2 |
| Stage 11: Backward Compat | 4h | P1 |
| Stage 12: Performance | 4h P2 |
| Stage 13: Verification | 2h | P1 |
| Stage 14: Release | 1h | P2 |
| **Total** | **69h** | |

---

## Success Criteria

- ✅ All 5 local ingestors preserve full tool call structure
- ✅ Cursor and Gemini sources added (match claude-replay coverage)
- ✅ Tool use IDs link calls to results
- ✅ No 500-char truncation anywhere
- ✅ Tool names normalized across platforms
- ✅ Patches/diffs preserved (not flattened)
- ✅ Error status tracked and searchable
- ✅ Verification script passes for all sources
- ✅ Backward compatibility maintained
- ✅ No performance regression
- ✅ Test coverage matches claude-replay
