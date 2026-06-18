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

1. Runs pre-flight checks: ruff, pytest, service smoke
2. Auto-detects version from conventional commits since last tag (`feat` → minor, `fix` → patch, breaking → major)
3. Bumps `pyproject.toml`, commits as `chore(main): release vX.Y.Z`, tags, pushes
4. Fetches the GitHub archive tarball and computes its SHA256 (local `git archive` produces different output)
5. Updates `Formula/llm-archive.rb` with new version, URL, and SHA, commits, pushes

Two commits per release: the version bump + tag, then the formula update.

After the tag is pushed, `.github/workflows/release.yml` auto-creates a GitHub Release with notes generated from conventional commits.

CI workflows run on the tag commit. If anything fails, delete the tag (`git tag -d vX.Y.Z && git push origin :vX.Y.Z`), fix, and rerun.

Overrides: `DRY_RUN=1 scripts/release.sh`

### Brew tap

The formula lives in this repo at `Formula/llm-archive.rb`. No separate tap repo.

```
brew tap shirk33y/llm-archive https://github.com/shirk33y/llm-archive
brew install llm-archive
```

On Homebrew ≥ 5.2, first-time install requires: `brew trust --formula shirk33y/llm-archive/llm-archive`

Alternative install for users with Python ≥ 3.11: `pipx install git+https://github.com/shirk33y/llm-archive.git`
