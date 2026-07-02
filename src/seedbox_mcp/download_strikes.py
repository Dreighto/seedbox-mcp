from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from seedbox_mcp.action_audit import rate_limit_exceeded, record_action
from seedbox_mcp.config import Settings
from seedbox_mcp.runtime import build_services

logger = logging.getLogger("seedbox_mcp.download_strikes")

# An item must be seen stalled this many consecutive monitor cycles before
# anything is removed. At the 30-min monitor cadence that's ~90 minutes of
# continuous stall — long enough that a transient blip (a torrent briefly
# losing peers, an indexer hiccup) recovers on its own and never accrues
# enough strikes to be touched. This is the whole point of the strike
# system: never act on a single observation. Community tools (Decluttarr,
# Cleanuparr) use the same count-before-acting pattern.
STRIKE_THRESHOLD = 3

# Strikes persist here across the monitor's oneshot runs (the service exits
# each cycle). Keyed by "{source}:{downloadId}" — downloadId is the client's
# own hash (SABnzbd nzo_id / torrent infohash), stable across polls, unlike
# the arr queue `id`.
STRIKE_STATE_PATH = Path(__file__).resolve().parent.parent.parent / ".monitor_download_strikes.json"

_STALL_MARKERS = ("stall", "no connections", "no files found", "no peers", "no seeds")
_IMPORT_MARKERS = ("import", "permission", "no files eligible")

# Sonarr/Radarr only inspect the 60 most recent items in the SABnzbd/NZBGet
# history when reconciling completed downloads; anything older than that
# window is silently skipped for import. With removeCompletedDownloads on
# (verified enabled here) the history stays tiny, so this never bites — but
# if that setting drifts off, or history balloons for another reason, older
# completed downloads start getting silently missed. Warn at 45, comfortably
# below the 60 cliff, so it's caught before any import is actually skipped.
SAB_HISTORY_WARN_THRESHOLD = 45


def sab_history_advisory(noofslots: int | None) -> str | None:
    """Pure: given SABnzbd's total-kept history count, return an advisory
    note if it's grown near the 60-item window Sonarr/Radarr reconcile
    against, else None."""
    if isinstance(noofslots, int) and noofslots >= SAB_HISTORY_WARN_THRESHOLD:
        return (
            f"SABnzbd download history has grown to {noofslots} items, approaching the 60-item "
            "window Sonarr/Radarr scan when reconciling completed downloads. Past 60, older "
            "finished downloads can be silently skipped for import. Check that 'Remove Completed' "
            "is still enabled on the SABnzbd client in both arrs (it keeps history small), or purge "
            "old history."
        )
    return None


def _messages_text(item: dict[str, Any]) -> str:
    parts = [str(item.get("errorMessage") or "")]
    for sm in item.get("statusMessages") or []:
        if isinstance(sm, dict):
            parts.append(str(sm.get("title") or ""))
            parts.extend(str(m) for m in (sm.get("messages") or []))
    return " ".join(parts).lower()


def classify_queue_item(item: dict[str, Any]) -> tuple[str | None, str]:
    """Returns (category, reason).

    category:
      'stalled'      — dead download, safe to remove+blocklist+re-search
      'import_issue' — stuck importing (usually PERMISSIONS); report only,
                       never auto-blocklist, because re-downloading won't
                       fix a permissions/path problem, it'll just loop
      None           — healthy / progressing normally
    """
    state = str(item.get("trackedDownloadState") or "").lower()
    tstatus = str(item.get("trackedDownloadStatus") or "").lower()
    status = str(item.get("status") or "").lower()
    msg = _messages_text(item)

    # Import problems first — an import-stuck item can also carry a warning
    # status, and we must NOT auto-blocklist those (re-downloading won't fix
    # a permissions/path problem). A real import issue shows up either as an
    # import-family download state or an import/permission message.
    is_import_state = state in ("importpending", "importblocked", "importfailed", "failedpending")
    if is_import_state or any(m in msg for m in _IMPORT_MARKERS):
        return "import_issue", f"stuck importing ({state or status or 'import error'})"

    if status in ("warning", "failed", "stalled") or tstatus in ("warning", "error"):
        detail = next((m for m in _STALL_MARKERS if m in msg), None)
        return "stalled", f"stalled ({detail or tstatus or status})"
    return None, ""


def _key(source: str, item: dict[str, Any]) -> str | None:
    dl = item.get("downloadId")
    return f"{source}:{dl}" if dl else None


def update_strikes(
    prev: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
    now_ts: float,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pure strike-state transition. Given the previous strike state and the
    current queue snapshot, returns:
      (new_state, to_act, import_issues)
    - to_act: stalled items that just reached STRIKE_THRESHOLD (act on these)
    - import_issues: items stuck importing (report only, not struck/acted)
    Healthy items and items that left the queue are dropped from state, so a
    download that recovers loses its accrued strikes rather than carrying
    them forever."""
    new_state: dict[str, dict[str, Any]] = {}
    to_act: list[dict[str, Any]] = []
    import_issues: list[dict[str, Any]] = []
    for item in items:
        key = _key(item.get("_source", ""), item)
        if not key:
            continue
        category, reason = classify_queue_item(item)
        if category == "import_issue":
            import_issues.append({**item, "_reason": reason})
            continue
        if category != "stalled":
            continue  # healthy → no entry, strikes reset
        strikes = int(prev.get(key, {}).get("strikes", 0)) + 1
        entry = {
            "strikes": strikes,
            "title": item.get("_title"),
            "reason": reason,
            "source": item.get("_source"),
            "queue_id": item.get("id"),
            "last_seen_ts": now_ts,
        }
        new_state[key] = entry
        if strikes >= STRIKE_THRESHOLD:
            to_act.append({**item, "_reason": reason, "_strikes": strikes})
    return new_state, to_act, import_issues


def _load_strikes() -> dict[str, dict[str, Any]]:
    if not STRIKE_STATE_PATH.exists():
        return {}
    try:
        loaded = json.loads(STRIKE_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("unreadable strike state at %s, starting fresh", STRIKE_STATE_PATH)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_strikes(state: dict[str, dict[str, Any]]) -> None:
    try:
        STRIKE_STATE_PATH.write_text(json.dumps(state))
    except OSError:
        logger.exception("failed to persist strike state to %s", STRIKE_STATE_PATH)


def _title_of(item: dict[str, Any]) -> str:
    movie = item.get("movie") or {}
    series = item.get("series") or {}
    return movie.get("title") or series.get("title") or item.get("title") or "unknown"


async def _fetch_queue(client: Any, source: str) -> list[dict[str, Any]]:
    unknown = "includeUnknownMovieItems" if source == "radarr" else "includeUnknownSeriesItems"
    q = await client.get("/api/v3/queue", {"page": 1, "pageSize": 200, unknown: "true"})
    records = q.get("records", []) if isinstance(q, dict) else (q if isinstance(q, list) else [])
    for r in records:
        r["_source"] = source
        r["_title"] = _title_of(r)
    return records


async def run_download_strike_check(settings: Settings, now_ts: float) -> str | None:
    """Deterministic stalled-download fixer, run before the LLM cycle (same
    pattern as monitor._deterministic_queue_resume). Reads the Radarr and
    Sonarr queues directly, strikes stalled items, and once an item has been
    stalled STRIKE_THRESHOLD cycles running, removes it from the queue AND
    the download client, blocklists the release so the same dead copy isn't
    re-grabbed, and lets the arr trigger a replacement search
    (skipRedownload=false). Returns a report note, or None if nothing
    changed and there's nothing to flag.

    now_ts is passed in (not read from the clock here) so the logic stays
    testable and deterministic."""
    services = build_services(settings)
    clients = [(services.radarr, "radarr"), (services.sonarr, "sonarr")]
    items: list[dict[str, Any]] = []
    for client, source in clients:
        if client is None:
            continue
        try:
            items.extend(await _fetch_queue(client, source))
        except Exception:
            logger.exception("failed to fetch %s queue for strike check", source)

    prev = _load_strikes()
    new_state, to_act, import_issues = update_strikes(prev, items, now_ts)

    acted: list[str] = []
    for item in to_act:
        if rate_limit_exceeded():
            logger.warning("strike check hit rate limit; deferring remaining removals")
            break
        client = services.radarr if item["_source"] == "radarr" else services.sonarr
        queue_id = item.get("id")
        params = {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "false"}
        try:
            await client.delete(f"/api/v3/queue/{queue_id}", params)
        except Exception:
            logger.exception("failed to remove stalled queue item %s", queue_id)
            continue
        record_action(
            f"{item['_source']}_queue_action",
            {"queue_id": queue_id, "action": "blocklist", "removeFromClient": True, "auto": "strike_threshold"},
            dry_run=False,
            outcome="ok",
        )
        key = _key(item["_source"], item)
        if key:
            new_state.pop(key, None)  # gone now — don't keep striking it
        acted.append(f'"{item["_title"]}" ({item["_reason"]}, {item["_strikes"]} strikes)')
        logger.info("strike check removed+blocklisted stalled download: %s", item["_title"])

    _save_strikes(new_state)

    notes: list[str] = []
    if acted:
        notes.append(
            "Auto-fixed stalled downloads (removed, blocklisted, re-searching) after "
            f"{STRIKE_THRESHOLD}+ cycles stalled: " + "; ".join(acted) + "."
        )
    if import_issues:
        titles = "; ".join(f'"{i["_title"]}" ({i["_reason"]})' for i in import_issues[:5])
        notes.append(
            f"{len(import_issues)} download(s) stuck on import, likely a permissions or path issue "
            f"(NOT auto-fixed, since re-downloading won't fix that): {titles}. Worth a look."
        )
    if services.sabnzbd is not None:
        try:
            hist = await services.sabnzbd.history(limit=1)
            advisory = sab_history_advisory((hist.get("history") or {}).get("noofslots"))
            if advisory:
                notes.append(advisory)
        except Exception:
            logger.exception("SABnzbd history advisory check failed (non-fatal)")
    return "\n".join(notes) if notes else None
