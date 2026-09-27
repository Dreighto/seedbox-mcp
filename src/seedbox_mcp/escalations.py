"""Issues the operator escalated from a NAS report, so the monitor stays quiet
about them while a worker is on it and tells the operator how it went.

The Escalate button records an entry; each monitor cycle reports and drops
entries whose worker has finished (or never answered), which lets a still-broken
issue alert again."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("seedbox_mcp.escalations")

ESCALATIONS_PATH = Path(__file__).resolve().parent.parent.parent / ".escalations.json"
# The dispatch listener kills a worker well before this; past it the result is not coming.
GIVE_UP_AFTER_S = 6 * 3600


def load() -> dict[str, dict[str, Any]]:
    try:
        loaded = json.loads(ESCALATIONS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save(entries: dict[str, dict[str, Any]]) -> None:
    try:
        ESCALATIONS_PATH.write_text(json.dumps(entries))
    except OSError:
        logger.exception("failed to persist escalations to %s", ESCALATIONS_PATH)


def record(key: str, title: str, trace_id: str, now_ts: float | None = None) -> None:
    entries = load()
    entries[key] = {"title": title, "trace_id": trace_id, "ts": now_ts if now_ts is not None else time.time()}
    _save(entries)


def active_keys() -> set[str]:
    return set(load())


def _outcome_text(title: str, result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").upper()
    lines = [line.strip() for line in str(result.get("summary") or "").splitlines() if line.strip()]
    confirmed = bool(lines) and lines[0].upper().startswith("STATUS: CONFIRMED WORKING")
    # Kernel workers open their report with a STATUS: line; the plain account follows it.
    detail = next((line for line in lines if not line.upper().startswith("STATUS:")), "")[:300].rstrip(".")
    if status in ("SUCCESS", "COMPLETED", "PASSED") or confirmed:
        return f'The worker you escalated "{title}" to fixed it: {detail or "no details given"}.'
    if result.get("closeout_class") == "timeout" or result.get("exit_code") == 143:
        why = "it ran out of time before finishing"
    else:
        why = detail or "it gave no report"
    return (
        f'The worker you escalated "{title}" to did not fix it: {why}. '
        "If it's still a problem, the next report will flag it again."
    )


def collect_finished(results_dir: Path, now_ts: float | None = None) -> list[str]:
    """Messages for the operator about escalations that are over, and drops
    them from the store. Unfinished ones stay (and stay quiet)."""
    now = now_ts if now_ts is not None else time.time()
    entries = load()
    messages: list[str] = []
    for key, entry in list(entries.items()):
        path = results_dir / f"{entry.get('trace_id')}.result.json"
        try:
            result = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            result = None
        if isinstance(result, dict):
            messages.append(_outcome_text(entry.get("title", key), result))
            del entries[key]
        elif now - float(entry.get("ts", 0)) >= GIVE_UP_AFTER_S:
            messages.append(
                f'No result came back for "{entry.get("title", key)}" after 6 hours. '
                "If it's still a problem, the next report will flag it again."
            )
            del entries[key]
    if messages:
        _save(entries)
    return messages
