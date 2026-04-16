# Refactoring Plan

## Goals

1. Reduce code duplication across web ingestors
2. Better organize codebase for future TUI and MCP development
3. Improve naming and structure clarity

---

## 1. Extract Shared Utilities

### New Files

| File | Content | From |
|------|---------|------|
| `llm_archive/text.py` | Content cleaning, injection tags, `_parse_parts()` | `db.py` |
| `llm_archive/ids.py` | `to_base53()`, `from_base53()` | `db.py` |

### Rationale

- `db.py` has unrelated utilities (ID encoding, text parsing)
- Extract to clear, focused modules
- No `utils/` directory - use domain-specific names

---

## 2. Rename `auth/` to `browser.py`

**Move**: `llm_archive/auth/playwright.py` → `llm_archive/browser.py`

**Keep**: `llm_archive/auth/__init__.py` (may add other auth methods later)

### Rationale

- Name reflects functionality (browser-based auth)
- `auth/` is too generic

---

## 3. Move `windsurf_protocol/` into `ingestors/windsurf/`

**Move**: `llm_archive/windsurf_protocol/*.py` → `llm_archive/ingestors/windsurf/`

### New Structure

```
llm_archive/ingestors/windsurf/
├── __init__.py
├── ingestor.py       # (renamed from windsurf.py)
├── extract_csrf_auto.py
├── extract_csrf_safe.py
├── extract_csrf_from_request.py
└── intercept_requests.py
```

### Rationale

- `windsurf_protocol/` is an awkward name
- `ingestors/windsurf/` follows existing pattern
- Keep helper modules for now (may consolidate later)

---

## 4. Create `sync.py` for Orchestration

**New**: `llm_archive/sync.py`

Extract from `cli.py`:
- `_sync()` function
- `_do_ingest()` function
- `_ingest_total()` function

Keep in `cli.py`:
- Click command definitions
- Display formatting

### Rationale

- MCP server will need to trigger syncs
- CLI and MCP should share sync logic
- TUI display logic stays CLI-specific (will diverge)

---

## 5. Extract Shared Web Ingestor Logic (Future)

### Duplication Found

| Pattern | ChatGPT | Claude | DeepSeek |
|---------|---------|--------|----------|
| Incremental sync | ✅ | ✅ | ✅ |
| Timestamp parsing | ✅ | ✅ | ✅ |
| 401/429 handling | ✅ | ✅ | ✅ |
| Rate limiting | ✅ (adaptive) | 1s fixed | none |

**Incremental sync logic is identical across all 3 web ingestors.**

### Phase 1: Extract to `ingestors/web_utils.py`

```python
def should_skip_conversation(
    thread_id: str,
    updated_at: int | None,
    existing_thread_ids: set[str] | dict[str, int],
) -> SkipReason:
    """Check if conversation should be skipped (already synced and up-to-date)."""
    ...

def parse_timestamp(value) -> int | None:
    """Parse various timestamp formats to Unix ms."""
    ...
```

### Phase 2: Consider `WebIngestor` Base Class (Later)

Only if it simplifies things, not for its own sake. Different APIs have quirks.

---

## 6. Public API in `__init__.py`

**Add to**: `llm_archive/__init__.py`

```python
from llm_archive import sync, connect, get_ingestor
```

### Rationale

- Clean import for MCP: `from llm_archive import sync`
- Hide internal structure

---

## 7. CLI Organization

**Decision**: Keep single `cli.py` for now.

Split when:
- 15+ commands
- TUI needs shared code
- Pain becomes real

---

## Final Structure

```
llm_archive/
├── __init__.py           # Public API
├── db.py                 # Database operations
├── schema.py             # Data models (IngestedThread, etc.)
├── text.py               # Content cleaning, parsing
├── ids.py                # ID encoding (base53)
├── sync.py               # Sync orchestration
├── browser.py            # Playwright utilities
│
├── ingestors/
│   ├── __init__.py
│   ├── base.py           # BaseIngestor
│   ├── chatgpt.py
│   ├── claude.py
│   ├── deepseek.py
│   ├── claudecode.py
│   ├── opencode.py
│   └── windsurf/         # (from windsurf_protocol/)
│       ├── __init__.py
│       ├── ingestor.py
│       └── ...
│
├── cli/
│   ├── __init__.py
│   └── commands.py       # (future split, not now)
│
└── tui/                  # (future)
    └── ...

scripts/                  # (future: windsurf helpers move here)
```

---

## Implementation Order

1. **Low risk**: Extract `text.py`, `ids.py`
2. **Rename**: `auth/` → `browser.py`
3. **Move**: `windsurf_protocol/` → `ingestors/windsurf/`
4. **Extract**: `sync.py` from `cli.py`
5. **Add**: Public API in `__init__.py`
6. **Phase 2**: Web ingestor utils extraction

---

## Non-Goals

- Don't abstract rate limiting yet (too different across ingestors)
- Don't split CLI until needed
- Don't create `utils/` directory
- Don't add TUI/MCP scaffolding preemptively
