#!/usr/bin/env bash
# Keep https://celo.uconfidant.tech answering, without a human watching it.
#
# The demo runs on this machine behind a Cloudflare *quick* tunnel: free, no
# account, but the connection drops (a reconnect window serves 530 to anyone
# holding a judge link) and the process can outlive its own usefulness. So probe
# the two things that can independently fail, in the cheap order:
#
#   1. the local app (localhost:$PORT/health) — if that is down, restart serve;
#   2. the public host (https://.../health) — if that is down twice in a row, the
#      tunnel is the problem: restart it. scripts/tunnel_sync.sh republishes the
#      site and the ERC-8004 card when the hostname rotates, so a restart heals
#      the proxy too.
#
# Two consecutive public failures, not one: a reconnect takes seconds, and
# bouncing the tunnel on a single blip would rotate the hostname for no reason.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PUBLIC_PORT:-8088}"
PUBLIC="${PUBLIC_URL:-https://celo.uconfidant.tech}"
FAILFILE="$REPO/.watchdog_public_failures"
LOG="$REPO/watchdog.log"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

code() {
  local c
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$1")
  [ -n "$c" ] || c=000
  printf '%s' "$c"
}

# WATCHDOG_DRY_RUN=1 logs the action instead of taking it, so the failure path can
# be tested without rotating a live tunnel.
act() {
  if [ "${WATCHDOG_DRY_RUN:-0}" = "1" ]; then
    log "dry-run: would run systemctl --user restart $1"
  else
    systemctl --user restart "$1" || log "restart $1 failed"
  fi
}

local_code=$(code "http://localhost:$PORT/health")
if [ "$local_code" != "200" ]; then
  log "local app /health -> $local_code; restarting impay-serve.service"
  act impay-serve.service
  sleep 8
  local_code=$(code "http://localhost:$PORT/health")
  log "after restart local /health -> $local_code"
fi

public_code=$(code "$PUBLIC/health")
if [ "$public_code" = "200" ]; then
  [ -f "$FAILFILE" ] && rm -f "$FAILFILE"
  exit 0
fi

fails=$(cat "$FAILFILE" 2>/dev/null || echo 0)
fails=$((fails + 1))
echo "$fails" > "$FAILFILE"

if [ "$fails" -lt 2 ]; then
  log "public /health -> $public_code (failure $fails/2); waiting for the reconnect"
  exit 0
fi

log "public /health -> $public_code twice; restarting impay-tunnel.service (republishes on rotation)"
act impay-tunnel.service
rm -f "$FAILFILE"
if [ "${WATCHDOG_DRY_RUN:-0}" = "1" ]; then
  exit 0
fi
sleep 20
log "after tunnel restart public /health -> $(code "$PUBLIC/health")"
