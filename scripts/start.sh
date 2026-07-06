#!/usr/bin/env bash
set -euo pipefail

# run this on the seedbox to kick off the mcp
# crontab entry:
# @reboot sleep 30 && bash ~/seedbox-mcp/scripts/start.sh

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MCP_SESSION="media-mcp"
LOG_MCP="$REPO/mcp.log"

# Kill existing session if running
screen -S "$MCP_SESSION"  -X quit 2>/dev/null && echo "Stopped $MCP_SESSION"  || true

# Brief pause to let ports release
sleep 1

screen -dmS "$MCP_SESSION"  bash -c "cd '$REPO' && uv run python -m seedbox_mcp.server      2>&1 | tee -a '$LOG_MCP'"
echo "Started $MCP_SESSION (logs: $LOG_MCP)"
