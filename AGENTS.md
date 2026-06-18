# llm-archive — AGENTS.md

## Commit convention

Use conventional commits: `type(scope): description`

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `ci`, `chore`, `style`, `perf`

Scope is optional — use the module or area (e.g. `ingestors`, `deps`, `plan`, `cli`).

Always lowercase. No period at end. Imperative mood.

Examples:
- `feat(cli): add --dry-run flag`
- `refactor(ingestors): remove playwright, keep CDP for windsurf only`
- `docs(plan): remove done items, update tree and order`
- `fix: show overdue duration instead of opaque "due" in NEXT column`

---

## Release process

Run `scripts/release.sh`:

  ```
  scripts/release.sh
  ```

The script:

- Runs pre-flight checks: ruff, pytest, service smoke
- Auto-detects version from conventional commits since last tag (`feat` → minor, `fix` → patch, breaking → major)
- Bumps `pyproject.toml` and `Formula/llm-archive.rb`
- Commits as `chore(main): release vX.Y.Z` and tags
- Pushes to GitHub (triggers CI on the tag commit)
- Computes archive SHA and updates brew formula

After the tag is pushed, `.github/workflows/release.yml` auto-creates a GitHub Release with notes generated from conventional commits.

CI workflows run on the tag commit. If anything fails, delete the tag (`git tag -d vX.Y.Z && git push origin :vX.Y.Z`), fix, and rerun.

Overrides: `DRY_RUN=1 scripts/release.sh`
