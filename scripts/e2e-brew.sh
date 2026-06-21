#!/usr/bin/env bash
set -euo pipefail

# dummy must stay invisible in the provider catalog
llm-archive status --json | jq -e '.sources | map(.id) | index("dummy") | not'

mkdir -p ~/.config/llm-archive
printf '%s\n' \
  '[ingestors.dummy]' \
  'enabled = true' \
  'sync_interval = "1s"' \
  'min_sync_interval = "1s"' \
  'watch = false' > ~/.config/llm-archive/config.toml

# Linuxbrew services drives systemd --user, which needs a user session.
if command -v systemctl >/dev/null 2>&1; then
  sudo loginctl enable-linger "$USER" 2>/dev/null || true
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  systemctl --user daemon-reload 2>/dev/null || true
fi
brew services start llm-archive
sleep 3
brew services info llm-archive

# Poll until dummy sync completes
for _ in $(seq 1 90); do
  llm-archive status --json > /tmp/status.json 2>/dev/null || true
  if scripts/check-service.sh enabled 2>/dev/null; then break; fi
  sleep 2
done
scripts/check-service.sh enabled

llm-archive search dummycanarytoken --json | jq -e '.count > 0'

# Embed + semantic search e2e
llm-archive embed --force 2>&1 | tee /tmp/embed.log
grep -q "embedded" /tmp/embed.log
llm-archive search -s "test query" --json | jq -e '.count > 0'

echo "brew e2e PASS"