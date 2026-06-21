#!/usr/bin/env bash
set -euo pipefail
mode=$1
now_ms=$(( $(date +%s) * 1000 ))
hb=$(jq -r '.service.heartbeat_at // 0' /tmp/status.json)
fresh=false
if [ "$hb" != "0" ] && [ $(( now_ms - hb )) -le 90000 ]; then fresh=true; fi
threads=$(jq '[.sources[] | select(.id=="dummy") | .thread_count // 0][0] // 0' /tmp/status.json)
success=$(jq '[.jobs[] | select(.source_id=="dummy" and .status=="success")] | length' /tmp/status.json)
if $fresh && [ "$mode" = "disabled" ] && [ "$threads" = "0" ] && [ "$success" = "0" ]; then
  echo "OK (disabled): threads=$threads success=$success"; exit 0
fi
if $fresh && [ "$mode" = "enabled" ] && [ "$success" != "0" ] && [ "$threads" -ge 1 ]; then
  echo "OK (enabled): threads=$threads success=$success"; exit 0
fi
echo "FAIL ($mode): fresh=$fresh threads=$threads success=$success" >&2; exit 1