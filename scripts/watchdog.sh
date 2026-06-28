#!/usr/bin/env bash
set -euo pipefail

# Periodic self-heal for the media-mcp + media-chat screen sessions, run from
# cron every 5 min. start.sh unconditionally kills+relaunches both, so we only
# invoke it when a port is actually down — otherwise we'd bounce live sessions
# on every tick. Mirrors the tautulli/overseerr watchdog crons on the seedbox.
#
# Why this exists: a Whatbox slot migration kills processes but does NOT re-fire
# user @reboot crons. seedbox-mcp previously had only an @reboot entry and no
# periodic watchdog, so it stayed down after the oberon->greip move while the
# watchdog'd services (tautulli, overseerr) recovered on their own.
#
# crontab entry:
#   */5 * * * * /home/wawa/seedbox-mcp/scripts/watchdog.sh >> /home/wawa/seedbox-mcp/watchdog.log 2>&1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MCP_PORT="${MCP_PORT:-17432}"
CHAT_PORT="${CHAT_PORT:-17433}"

# A successful connection (any HTTP response) means the port is bound and
# serving; that is the signal we care about, not the specific status code.
probe() { curl -s -o /dev/null --max-time 5 "http://127.0.0.1:$1$2"; }

if probe "$MCP_PORT" "/health" && probe "$CHAT_PORT" "/"; then
  exit 0
fi

echo "$(date -Is) watchdog: mcp or chat is down — running start.sh"
bash "$REPO/scripts/start.sh"
