# Semantic Search

FTS5 (keyword search) fails for synonyms and paraphrases: searching "processor" won't find "CPU", "auth bug" won't find "login broken". Semantic search fixes this by comparing meaning, not tokens.

## Architecture

```
Thread ingested
     │
     ▼
llm-archive embed          ← CLI command, run after sync
     │
     ├── extract_thread_text()   title + first N user/assistant messages (skip tool calls)
     ├── ollama /api/embeddings  model: nomic-embed-text (768d)
     └── store in sqlite-vec     vec_threads virtual table (ANN index)

At query time (MCP: semantic_search)
     │
     ├── embed query string
     ├── ANN lookup in vec_threads (cosine distance)
     └── JOIN threads → return ranked results with distance score
```

## Components

**sqlite-vec** — SQLite C extension, vector similarity search virtual table (`vec0`). No separate process, single SQLite file, transactional. Fast enough for personal scale (tens of thousands of threads).

**ollama** — local model server. Embedding model runs on CPU, no GPU needed.

**nomic-embed-text** — default model. 768d, ~270MB, excellent quality/size tradeoff.

| Model | Dims | Size | Notes |
|---|---|---|---|
| `nomic-embed-text` | 768 | 270MB | default, recommended |
| `mxbai-embed-large` | 1024 | 670MB | higher quality |
| `all-minilm` | 384 | 45MB | fast, lower quality |

## Setup

```sh
# 1. Install sqlite-vec
pip install sqlite-vec

# 2. Install ollama + pull model
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull nomic-embed-text

# 3. Sync first, then embed
llm-archive sync
llm-archive embed

# 4. Search via MCP tool: semantic_search(query="cpu optimization")
# or via CLI:
llm-archive search --semantic "cpu optimization"
```

Options:
```sh
llm-archive embed [SOURCE]               # embed specific source only
llm-archive embed --force                # re-embed all (model change, etc.)
llm-archive embed --model mxbai-embed-large
llm-archive embed --ollama-url http://remote:11434
```

## Schema

```sql
-- Metadata + rowid mapping
CREATE TABLE thread_embeddings (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT UNIQUE NOT NULL,
    model       TEXT NOT NULL,
    embedded_at INTEGER NOT NULL
);

-- Vector ANN index (rowid matches thread_embeddings.rowid)
CREATE VIRTUAL TABLE vec_threads USING vec0(embedding FLOAT[768]);
```

## Search result fields

```json
{
  "thread_id": "claudecode:abc123",
  "distance": 0.15,          // 0 = perfect match, higher = less similar
  "title": "...",
  "source_id": "claudecode",
  "updated_at": 1234567890
}
```

Distance < 0.3 = highly relevant. Distance > 0.6 = probably unrelated.

## Graceful degradation

- sqlite-vec not installed → `semantic_search` returns `{"error": "...", "results": []}` (FTS5 still works)
- ollama not running → same error pattern, no crash
- Thread not embedded → excluded from results (run `llm-archive embed` to index)

## Future: Phase 2 (summaries)

Embedding raw thread content is noisy. Better: generate per-thread summaries via local LLM (ollama + llama3.2:3b), embed summaries. Planned fields:

```sql
ALTER TABLE thread_embeddings ADD COLUMN short_desc TEXT;
ALTER TABLE thread_embeddings ADD COLUMN topics TEXT;   -- JSON array
```

## Future: Phase 3 (auto-context)

Claude Code hook: on new conversation start → `semantic_search(first_user_message)` → inject top-3 relevant threads as context. Replicates claude.ai memory behavior locally.
