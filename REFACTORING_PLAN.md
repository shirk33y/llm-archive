# Refactoring Plan

## Goals

1. Reduce code duplication across web ingestors
2. Better organize codebase for future TUI and MCP development
3. Improve naming and structure clarity

---

## Remaining: Shared Web Ingestor Logic

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
    ...

def parse_timestamp(value) -> int | None:
    ...
```

### Phase 2: Consider `WebIngestor` Base Class (Later)

Only if it simplifies things, not for its own sake. Different APIs have quirks.

---

## Non-Goals

- Don't abstract rate limiting yet (too different across ingestors)
- Don't create `utils/` directory
- Don't add TUI/MCP scaffolding preemptively
