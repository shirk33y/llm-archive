#!/usr/bin/env bash
set -euo pipefail
export HOME="$TEST_HOME"
LA="$GITHUB_WORKSPACE/.venv/bin/llm-archive"
CFG="$HOME/.config/llm-archive/config.toml"
mkdir -p "$(dirname "$CFG")"

write_config() {
  printf '%s\n' \
    '[ingestors.dummy]' \
    "enabled = $1" \
    'sync_interval = "1s"' \
    'min_sync_interval = "1s"' \
    'watch = false' > "$CFG"
}

poll() {
  local mode="$1" tries="${2:-45}"
  for _ in $(seq 1 "$tries"); do
    "$LA" status --json > /tmp/status.json 2>/dev/null || true
    if scripts/check-service.sh "$mode" 2>/dev/null; then return 0; fi
    sleep 2
  done
  echo "::error::poll timed out ($mode)"
  echo "service pid: $(cat "$RUNNER_TEMP/svc.pid" 2>/dev/null)"
  kill -0 "$(cat "$RUNNER_TEMP/svc.pid" 2>/dev/null)" 2>&1 && echo "service alive" || echo "service DEAD"
  "$LA" status --json || true
  scripts/check-service.sh "$mode" || true
  return 1
}

# dummy must stay invisible in the provider catalog
"$LA" status --json | jq -e '.sources | map(.id) | index("dummy") | not'

# Phase 1: disabled. Service must heartbeat but must NOT sync dummy.
write_config false
"$LA" service > "$RUNNER_TEMP/svc.log" 2>&1 &
echo $! > "$RUNNER_TEMP/svc.pid"
sleep 3
echo "service started pid=$(cat "$RUNNER_TEMP/svc.pid")"
poll disabled 20

# Phase 2: enable. Service must pick up the live config change.
write_config true
poll enabled 60

# CLI smoke against the running service's database
"$LA" search dummycanarytoken --json | jq -e '.count > 0'
"$LA" logs | grep -q dummy

# Embed + semantic search e2e
"$LA" embed --force 2>&1 | tee /tmp/embed.log
grep -q "embedded" /tmp/embed.log
"$LA" search -s "test query" --json | jq -e '.count > 0'

echo "e2e PASS"