# llm-archive — AGENTS.md

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
brew tap shirk33y/llm-archive https://github.com/shirk33y/llm-archive
brew trust --formula shirk33y/llm-archive/llm-archive   # Homebrew ≥ 5.2
brew install llm-archive
```

pipx: `pipx install git+https://github.com/shirk33y/llm-archive.git` (Python ≥ 3.11)