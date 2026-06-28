# llm-archive — AGENTS.md

## Setup

After cloning: `uv sync --extra dev && uv run pre-commit install`

## Tests

Use temporary databases for all tests and verification. Do not run test operations against the user's configured archive database unless explicitly asked.

## User DB

Live archive DB defaults to `~/.llm-archive/archive.db`; override with `LLM_ARCHIVE_DB`. Backups live in `~/.llm-archive/backups/`; exported thread markdown lives in `~/.llm-archive/exports/`.

Use read-only SQLite for diagnosis:

```sh
sqlite3 'file:'"$HOME"'/.llm-archive/archive.db?mode=ro' 'PRAGMA quick_check;'
```

Schema/count glimpse:

```sql
SELECT
  s.id,
  s.hostname,
  s.last_sync,
  ps.enabled,
  ps.stale_since,
  ps.pending_events,
  ps.last_sync_started_at,
  ps.last_sync_finished_at,
  ps.last_success_at,
  ps.next_sync_at,
  ps.last_error,
  ps.failure_count,
  ps.auth_status,
  ps.path_status,
  ps.watch_active,
  ps.watch_seen_at,
  ps.watch_error,
  COUNT(DISTINCT t.id) AS threads,
  COUNT(m.id) AS messages,
  MAX(t.updated_at) AS newest_thread
FROM sources s
LEFT JOIN provider_state ps ON ps.source_id = s.id
LEFT JOIN threads t ON t.source_id = s.id
LEFT JOIN messages m ON m.thread_id = t.id
GROUP BY s.id
ORDER BY threads DESC, s.id;

SELECT
  j.id,
  j.kind,
  j.source_id,
  j.status,
  j.reason,
  j.started_at,
  j.heartbeat_at,
  j.finished_at,
  j.force,
  j.error
FROM jobs j
ORDER BY j.started_at DESC
LIMIT 20;
```

## Commits

`type(scope): description` — lowercase, imperative, no trailing period. Scope optional.

## Release

`scripts/release.sh` — dry run: `DRY_RUN=1`

1. Preflight: ruff, pytest, service smoke
2. Bump `pyproject.toml` from conventional commits → commit + tag → push
3. Fetch GitHub archive SHA (local `git archive` differs) → update `Formula/llm-archive.rb` → push

Two commits per release. Rollback: `git tag -d vX.Y.Z && git push origin :vX.Y.Z`

Tag push triggers `.github/workflows/release.yml` (auto GitHub Release).

## Install

```
brew install --HEAD ./Formula/llm-archive.rb
```

pipx: `pipx install git+https://github.com/shirk33y/llm-archive.git` (Python ≥ 3.11)

## Embeddings

- fastembed only (BAAI/bge-small-en-v1.5, 384d), no ollama
- Auto-embed after sync by default (`[embed] auto = true` in config)
- `auto_embed()` skips if no embeddings exist yet (user hasn't bootstrapped)
- sqlite-vec KNN requires `AND k = ?` syntax (not `LIMIT ?`) with JOINs
- Dimension mismatch: warns user, requires `--force` to rebuild
- Thread-level embeddings with role prefixes (`title:`, `user:`, `assistant:`)
- Batch embedding (256 per batch) via fastembed

## Language

Always respond in English unless the user explicitly asks for Polish. Never write in Chinese or other languages.
