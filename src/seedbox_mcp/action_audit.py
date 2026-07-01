from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("seedbox_mcp.action_audit")

# Append-only audit trail for ACTION_TOOLS/ESCALATION_TOOLS calls specifically
# — not every read-only lookup, that would dilute the signal. One line per
# call: what was asked for, whether it was a dry-run or the real thing, and
# the outcome. Gitignored (same treatment as .telegram_bot_history.json) —
# this is operational data, not something that belongs in the repo.
AUDIT_LOG_PATH = Path(__file__).resolve().parent.parent.parent / ".action_audit.jsonl"

# Circuit breaker: caps REAL (confirm=true) executions per rolling hour,
# independent of the model's own judgment — a prompt can't talk its way
# around this. Dry-run previews don't count; they don't change anything.
# The number is deliberately generous (a legitimate burst of real fixes
# during an incident shouldn't trip it) but low enough to stop a genuine
# runaway loop cold.
MAX_ACTIONS_PER_HOUR = 20


def record_action(tool: str, args: dict[str, Any], dry_run: bool, outcome: str) -> None:
    entry = {
        "ts": time.time(),
        "tool": tool,
        "args": args,
        "dry_run": dry_run,
        "outcome": outcome,
    }
    try:
        with AUDIT_LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        logger.exception("failed to write action audit entry for %s", tool)


def recent_real_action_count(window_s: float = 3600.0) -> int:
    """How many REAL (non-dry-run) actions landed in the last `window_s`
    seconds — read fresh from disk every call, not cached, so this can't
    drift from what actually happened across restarts."""
    if not AUDIT_LOG_PATH.exists():
        return 0
    cutoff = time.time() - window_s
    count = 0
    try:
        with AUDIT_LOG_PATH.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not entry.get("dry_run") and entry.get("ts", 0) >= cutoff:
                    count += 1
    except OSError:
        logger.exception("failed to read action audit log for rate-limit check")
        return 0
    return count


def rate_limit_exceeded() -> bool:
    return recent_real_action_count() >= MAX_ACTIONS_PER_HOUR
