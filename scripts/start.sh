#!/usr/bin/env bash
set -euo pipefail

# run this on the seedbox to kick off both the mcp and chat
# crontab entry:
# @reboot sleep 30 && bash ~/seedbox-mcp/scripts/start.sh

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MCP_SESSION="media-mcp"
CHAT_SESSION="media-chat"
LOG_MCP="$REPO/mcp.log"
LOG_CHAT="$REPO/chat.log"

# Kill existing sessions if running
screen -S "$MCP_SESSION"  -X quit 2>/dev/null && echo "Stopped $MCP_SESSION"  || true
screen -S "$CHAT_SESSION" -X quit 2>/dev/null && echo "Stopped $CHAT_SESSION" || true

# Brief pause to let ports release
sleep 1

screen -dmS "$MCP_SESSION"  bash -c "cd '$REPO' && uv run python -m seedbox_mcp.server      2>&1 | tee -a '$LOG_MCP'"
echo "Started $MCP_SESSION (logs: $LOG_MCP)"

screen -dmS "$CHAT_SESSION" bash -c "cd '$REPO' && uv run python -m seedbox_mcp.chat.server 2>&1 | tee -a '$LOG_CHAT'"
echo "Started $CHAT_SESSION (logs: $LOG_CHAT)"
