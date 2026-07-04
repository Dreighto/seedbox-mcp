from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("seedbox_mcp.friend_tracking")

# Runtime state (gitignored): held friend requests we're watching so we can
# ping the requester when their request is approved and when it's ready to
# watch. One entry per (service, arr_id); dropped once "ready" fires or the
# item is declined/removed.
STORE_PATH = Path(__file__).resolve().parent.parent.parent / ".friend_request_tracking.json"


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(STORE_PATH.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(entries: list[dict[str, Any]]) -> None:
    try:
        STORE_PATH.write_text(json.dumps(entries, indent=2))
    except OSError:
        logger.exception("failed to persist friend tracking store")


def track_request(
    *,
    chat_id: int,
    name: str,
    service: str,
    arr_id: int,
    tmdb_id: int,
    kind: str,
    title: str,
    since: str,
) -> None:
    """Record a held friend request to watch. No-op without a chat_id or arr_id
    (nothing to notify / nothing to poll). De-dupes on (service, arr_id)."""
    if not chat_id or not arr_id:
        return
    entries = [e for e in _load() if not (e.get("service") == service and e.get("arr_id") == arr_id)]
    entries.append(
        {
            "chat_id": chat_id,
            "name": name,
            "service": service,
            "arr_id": arr_id,
            "tmdb_id": tmdb_id,
            "kind": kind,
            "title": title,
            "approved_notified": False,
            "since": since,
        }
    )
    _save(entries)


def list_tracked() -> list[dict[str, Any]]:
    return _load()


def save_tracked(entries: list[dict[str, Any]]) -> None:
    _save(entries)
