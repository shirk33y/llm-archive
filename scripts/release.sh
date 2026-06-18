#!/usr/bin/env bash
#
# Release script for llm-archive.
#
# Pre-flight: ruff check + pytest + service smoke
# Version: auto-detect from conventional commits since last tag
# Bump: pyproject.toml → commit → tag → push
# Brew: update Formula/llm-archive.rb, copy to tap repo, push
#
# Usage:  scripts/release.sh
#
# Overrides:
#   TAP_DIR   path to homebrew-tap clone  (default: ~/homebrew-tap)
#   DRY_RUN   set to 1 to skip push steps
#
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

TAP_DIR="${TAP_DIR:-/home/linuxbrew/.linuxbrew/Homebrew/Library/Taps/shirk33y/homebrew-tap}"
DRY_RUN="${DRY_RUN:-0}"

# ── Help ──────────────────────────────────────────────────────────────────
usage() {
  sed -n '3,11p' "$0" | sed 's/^#//; s/^ //'
  exit 0
}

# ── Utils ─────────────────────────────────────────────────────────────────
info()  { printf '\e[32m✓\e[0m %s\n' "$*"; }
warn()  { printf '\e[33m!\e[0m %s\n' "$*"; }
err()   { printf '\e[31m✗\e[0m %s\n' "$*"; exit 1; }
cmd()   { printf '\n  \e[2m$\e[0m \e[97m%s\e[0m\n' "$*"; "$@"; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  \e[2m[dry-run] %s\e[0m\n' "$*"
  else
    cmd "$@"
  fi
}

# ── 0. Pre-flight checks ──────────────────────────────────────────────────
preflight() {
  echo "Running pre-flight checks..."

  command -v curl >/dev/null || err "curl required (for archive SHA)"
  command -v git >/dev/null  || err "git required"

  cmd uv run ruff check  || err "ruff check failed"
  info "ruff passed"

  cmd uv run pytest -q --no-header 2>&1 | tail -5 \
    || err "pytest failed"
  info "unit tests passed"

  # Quick service smoke: does it start and heartbeat?
  echo "Starting service smoke..."
  TMPCFG=$(mktemp /tmp/llm-archive-release-config.XXXXXX)
  printf '[ingestors.dummy]\nenabled = false\nsync_interval = "1s"\nmin_sync_interval = "1s"\nwatch = false\n' > "$TMPCFG"
  mkdir -p /tmp/llm-archive-release-home
  _SAVED_HOME="$HOME"
  export HOME=/tmp/llm-archive-release-home
  mkdir -p "$HOME/.config/llm-archive"
  cp "$TMPCFG" "$HOME/.config/llm-archive/config.toml"
  rm -f "$TMPCFG"

  uv run llm-archive service > /tmp/llm-archive-release-svc.log 2>&1 &
  SVC_PID=$!
  sleep 3
  if ! kill -0 "$SVC_PID" 2>/dev/null; then
    cat /tmp/llm-archive-release-svc.log
    err "service failed to start"
  fi
  uv run llm-archive status --json > /tmp/llm-archive-release-status.json 2>/dev/null || true
  kill "$SVC_PID" 2>/dev/null || true
  wait "$SVC_PID" 2>/dev/null || true

  HB=$(python3 -c "
import json
with open('/tmp/llm-archive-release-status.json') as f:
    d = json.load(f)
hb = (d.get('service') or {}).get('heartbeat_at') or 0
print('ok' if hb else '')
")
  [ -n "$HB" ] || err "service status missing heartbeat"
  HOME="$_SAVED_HOME"
  info "service smoke passed"
}

# ── 1. Detect next version ──────────────────────────────────────────────
detect_version() {
  LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
  LAST_VER="${LAST_TAG#v}"

  COMMITS=$(git log "${LAST_TAG}..HEAD" --oneline 2>/dev/null || true)
  if [ -z "$COMMITS" ]; then
    err "no commits since $LAST_TAG — nothing to release"
  fi

  BUMP="patch"
  while IFS= read -r line; do
    msg=$(echo "$line" | sed 's/^[a-f0-9]*\s*//')

    # Breaking change: footer / ! after type
    if echo "$msg" | grep -qi 'BREAKING CHANGE' \
      || echo "$msg" | grep -qE '^[a-z]+!(\s|:)'; then
      BUMP="major"
      break
    fi

    # Feat → minor (if not already major)
    if echo "$msg" | grep -qE '^feat(\(.*\))?!?:'; then
      if [ "$BUMP" != "major" ]; then
        BUMP="minor"
      fi
    fi
  done <<< "$COMMITS"

  IFS='.' read -r MAJ MIN PAT <<< "$LAST_VER"
  case "$BUMP" in
    major) MAJ=$((MAJ + 1)); MIN=0; PAT=0 ;;
    minor) MIN=$((MIN + 1)); PAT=0 ;;
    patch) PAT=$((PAT + 1)) ;;
  esac
  NEXT_VER="${MAJ}.${MIN}.${PAT}"
  NEXT_TAG="v${NEXT_VER}"

  echo "Last tag:  $LAST_TAG"
  echo "Commits:   $(echo "$COMMITS" | wc -l)"
  echo "Bump:      $BUMP"
  echo "Next:      $NEXT_TAG"
  echo ""
}

# ── 2. Bump version in pyproject.toml ───────────────────────────────────
bump_version() {
  sed -i "s/^version = \".*\"/version = \"${NEXT_VER}\"/" pyproject.toml
  info "pyproject.toml → $NEXT_VER"
}

# ── 3. Build archive + compute SHA ──────────────────────────────────────
compute_sha() {
  REPO_URL="https://github.com/shirk33y/llm-archive"
  # Simulate: GitHub generates archive after push. We fetch from the archive URL.
  # But the tag doesn't exist yet locally (it will after push).
  # We need the tag to exist on GitHub first. So sequence matters:
  #   push → compute SHA → update formula → push formula

  # Alternative: compute SHA from local git archive (same result as GitHub).
  rm -rf /tmp/llm-archive-release-archive
  cmd git archive --format=tar.gz --prefix="llm-archive-${NEXT_VER}/" \
    -o "/tmp/llm-archive-release-archive.tar.gz" HEAD
  SHA=$(sha256sum /tmp/llm-archive-release-archive.tar.gz | cut -d' ' -f1)
  rm -f /tmp/llm-archive-release-archive.tar.gz
  info "archive SHA256: $SHA"
}

# ── 4. Update brew formula ─────────────────────────────────────────────
update_formula() {
  FORMULA="Formula/llm-archive.rb"

  # Check formula content (expected format)
  grep -q "version \"" "$FORMULA" || err "unexpected formula format (version)"

  # In-place update: version + sha256 + url
  ARCHIVE_URL="${REPO_URL}/archive/refs/tags/${NEXT_TAG}.tar.gz"

  sed -i 's|url ".*archive/refs/tags/v.*.tar.gz"|url "'"$ARCHIVE_URL"'"|' "$FORMULA"
  sed -i 's|sha256 ".*"|sha256 "'"$SHA"'"|' "$FORMULA"
  sed -i 's/version ".*"/version "'"$NEXT_VER"'"/' "$FORMULA"

  info "$FORMULA updated"
}

# ── 5. Commit, tag, push ───────────────────────────────────────────────
commit_and_tag() {
  run git add pyproject.toml "$FORMULA"
  run git commit -m "chore(main): release ${NEXT_TAG}"
  run git tag "$NEXT_TAG" -m "llm-archive ${NEXT_TAG}"
  run git push origin main --tags
}

# ── 6. Copy + push brew formula to tap repo ────────────────────────────
push_tap() {
  if [ ! -d "$TAP_DIR" ]; then
    warn "tap dir $TAP_DIR not found — skipping tap push"
    warn "  manually: cp Formula/llm-archive.rb <tap>/Formula/ && git -C <tap> add -A && git -C <tap> commit && git -C <tap> push"
    return
  fi

  run cp "Formula/llm-archive.rb" "$TAP_DIR/Formula/llm-archive.rb"
  run git -C "$TAP_DIR" add -A
  run git -C "$TAP_DIR" commit -m "llm-archive ${NEXT_TAG}"
  run git -C "$TAP_DIR" push
}

# ── Main ──────────────────────────────────────────────────────────────────
main() {
  if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
  fi

  # Confirm non-destructive intent
  LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
  echo "Releasing from: $(git rev-parse --short HEAD)"
  echo "Last tag:       $LAST_TAG"
  echo "DRY_RUN:        $DRY_RUN"
  echo "TAP_DIR:        $TAP_DIR"
  echo ""

  preflight

  echo ""
  echo "Detecting version..."
  detect_version

  # Confirm
  echo "Ready to release $NEXT_TAG."
  echo "Version bump will be committed, tagged, and pushed."
  echo "Press Enter to continue or Ctrl-C to abort..."
  read -r

  bump_version
  compute_sha
  update_formula
  commit_and_tag
  push_tap

  echo ""
  info "Released ${NEXT_TAG} 🚀"
  echo ""
  echo "CI will run on the tag commit. Monitor at:"
  echo "  https://github.com/shirk33y/llm-archive/actions"
  echo ""
  echo "When CI passes, the release workflow will auto-publish:"
  echo "  https://github.com/shirk33y/llm-archive/releases"
}

main "$@"
