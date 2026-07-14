from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from seedbox_mcp.action_audit import rate_limit_exceeded, record_action
from seedbox_mcp.config import Settings
from seedbox_mcp.runtime import Services, build_services

logger = logging.getLogger("seedbox_mcp.quality_guard")

# Radarr/Sonarr's quality-profile "allowed" gate is enforced at grab-decision
# time, parsed from the release NAME — it is NOT re-checked once the actual
# file is inspected on import. A release can be named to look like a normal
# 1080p encode and still land as BR-DISK or Remux after download. Verified
# live 2026-07-13/14: Super Mario Bros. (1993) landed as Remux-1080p despite
# its profile disallowing Remux, and Frankenstein (2025) landed as BR-DISK
# THREE separate times from three different indexer copies of an identically
# named release — each blocklist only stopped that one indexer's copy, not
# the other mirrors. This module is the safety net profile config alone
# cannot provide: it re-checks every newly imported file's ACTUAL landed
# quality/customFormatScore against policy, and undoes the import if it
# violates policy, instead of waiting for a human to notice a Telegram ping.

STATE_PATH = Path(__file__).resolve().parent.parent.parent / ".monitor_quality_guard_state.json"

# A matched custom-format score at or below this is treated as "this exact
# combination is intentionally vetoed" — mirrors the -10000 convention
# already used for the BR-DISK, Remux, and exact-title-block custom formats
# (see Radarr custom format ids 72, 114, 115 as of 2026-07-13). Not a normal
# upgrade-preference score.
HARD_BLOCK_SCORE = -1000

# After this many auto-corrections for the SAME exact release title, stop
# just re-searching (which can loop forever if the same content is mirrored
# across indexers under one name — the Frankenstein case) and instead add a
# permanent exact-title custom format block so it can never be grabbed again
# from any source.
REPEAT_OFFENDER_THRESHOLD = 2

# Defense in depth: these quality tiers are never acceptable regardless of
# what a profile's custom formats say, in case a profile is ever misconfigured
# again the way the "Any" profile was.
_QUALITY_BAD_TIERS = {
    "WORKPRINT",
    "CAM",
    "TELESYNC",
    "TELECINE",
    "REGIONAL",
    "DVDSCR",
    "DVD-R",
    "BR-DISK",
}


def _load_state() -> dict[str, Any]:
    default: dict[str, Any] = {"last_history_id": {"radarr": 0, "sonarr": 0}, "offenses": {}}
    if not STATE_PATH.exists():
        return default
    try:
        loaded = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("unreadable quality-guard state at %s, starting fresh", STATE_PATH)
        return default
    if not isinstance(loaded, dict):
        return default
    loaded.setdefault("last_history_id", {"radarr": 0, "sonarr": 0})
    loaded.setdefault("offenses", {})
    return loaded


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state))
    except OSError:
        logger.exception("failed to persist quality-guard state to %s", STATE_PATH)


def evaluate_import(quality_name: str, custom_formats: list[dict[str, Any]], custom_format_score: int) -> str | None:
    """Pure. Returns a violation reason, or None if the landed quality/format
    combination is fine. Two independent checks: a hard-coded disallowed-tier
    list (catches a tier even if a profile is ever misconfigured again) and
    the actual matched custom-format score (catches Remux, an exact-title
    block, or anything else scored via custom format rather than the
    quality-tier ladder)."""
    if quality_name in _QUALITY_BAD_TIERS:
        return f"quality tier {quality_name!r} is never acceptable regardless of profile"
    if custom_format_score <= HARD_BLOCK_SCORE:
        names = ", ".join(cf.get("name", "?") for cf in custom_formats) or "unnamed"
        return f"matched hard-blocked custom format(s) [{names}] (score {custom_format_score})"
    return None


async def _fetch_new_imports(client: Any, since_id: int) -> list[dict[str, Any]]:
    """downloadFolderImported is eventType 3 in both Radarr and Sonarr's
    history API (verified live 2026-07-13). History is newest-first; one page
    (200 records) comfortably covers a 30-min gap between monitor cycles
    under normal grab volume."""
    hist = await client.get(
        "/api/v3/history",
        {"page": 1, "pageSize": 200, "sortKey": "date", "sortDirection": "descending", "eventType": 3},
    )
    records = hist.get("records", []) if isinstance(hist, dict) else (hist if isinstance(hist, list) else [])
    return [r for r in records if r.get("id", 0) > since_id]


async def _find_grab_record_id(client: Any, source: str, entity_id: int, download_id: str | None) -> int | None:
    """The 'grabbed' history record for the same download, so it can be
    marked failed (blocklisted). Matched by downloadId, which is stable
    across a download's grab->import lifecycle."""
    if not download_id:
        return None
    path = f"/api/v3/history/movie?movieId={entity_id}&eventType=1" if source == "radarr" \
        else f"/api/v3/history/series?seriesId={entity_id}&eventType=1"
    records = await client.get(path)
    records = records if isinstance(records, list) else []
    for r in records:
        if r.get("downloadId") == download_id:
            rid = r.get("id")
            return int(rid) if rid is not None else None
    return None


async def _undo_bad_import(
    services: Services, client: Any, source: str, entity_id: int, record: dict[str, Any]
) -> None:
    """Deletes the bad file, blocklists the release that produced it, and
    triggers a fresh search. Radarr is movie-scoped; Sonarr is episode-scoped
    (a series can have many episode files, so the specific episode from this
    history record is targeted, not the whole series)."""
    download_id = record.get("downloadId")
    grab_id = await _find_grab_record_id(client, source, entity_id, download_id)
    if grab_id is not None:
        await client.post(f"/api/v3/history/failed/{grab_id}", {})

    if source == "radarr":
        movie = await client.get(f"/api/v3/movie/{entity_id}")
        movie_file = movie.get("movieFile") if isinstance(movie, dict) else None
        file_id = movie_file.get("id") if isinstance(movie_file, dict) else None
        if file_id is not None:
            await client.delete(f"/api/v3/moviefile/{file_id}")
        await client.post("/api/v3/command", {"name": "MoviesSearch", "movieIds": [entity_id]})
        return

    episode_id = record.get("episodeId")
    if episode_id is not None:
        episode = await client.get(f"/api/v3/episode/{episode_id}")
        file_id = episode.get("episodeFileId") if isinstance(episode, dict) else None
        if file_id:
            await client.delete(f"/api/v3/episodefile/{file_id}")
        await client.post("/api/v3/command", {"name": "EpisodeSearch", "episodeIds": [episode_id]})
    else:
        await client.post("/api/v3/command", {"name": "SeriesSearch", "seriesId": entity_id})


_TITLE_BLOCK_NAME_PREFIX = "Auto quality-guard block: "


async def _block_exact_title(client: Any, source: str, entity_id: int, title: str) -> bool:
    """Creates an exact-title-match custom format (score -10000, same
    convention as the manual Frankenstein fix) and adds it to whichever
    quality profile the entity currently uses. Returns True if applied."""
    cf_name = _TITLE_BLOCK_NAME_PREFIX + title
    existing = await client.get("/api/v3/customformat")
    existing = existing if isinstance(existing, list) else []
    if any(cf.get("name") == cf_name for cf in existing):
        return False  # already blocked — don't create a duplicate

    pattern = "(?i)^" + re.escape(title) + "$"
    created = await client.post(
        "/api/v3/customformat",
        {
            "name": cf_name,
            "includeCustomFormatWhenRenaming": False,
            "specifications": [
                {
                    "name": "exact title",
                    "implementation": "ReleaseTitleSpecification",
                    "negate": False,
                    "required": True,
                    "fields": [{"name": "value", "value": pattern}],
                }
            ],
        },
    )
    cf_id = created.get("id") if isinstance(created, dict) else None
    if cf_id is None:
        return False

    entity_path = f"/api/v3/{'movie' if source == 'radarr' else 'series'}/{entity_id}"
    entity = await client.get(entity_path)
    profile_id = entity.get("qualityProfileId") if isinstance(entity, dict) else None
    if profile_id is None:
        return False
    profile = await client.get(f"/api/v3/qualityprofile/{profile_id}")
    if not isinstance(profile, dict):
        return False
    profile.setdefault("formatItems", []).append({"format": cf_id, "name": cf_name, "score": -10000})
    await client.put(f"/api/v3/qualityprofile/{profile_id}", profile)
    return True


async def _bootstrap_high_water_mark(client: Any) -> int:
    """Newest history id right now, with no evaluation. Used only the very
    first time the guard runs for an app (state file doesn't exist yet) —
    without this, a fresh install would replay the app's ENTIRE recent import
    history on its first cycle, including anything already manually fixed
    before the guard ever ran. Evaluating a stale history record against a
    movie/episode whose file has since been replaced with a good one would
    delete that good file by mistake, since a history record is a snapshot
    of what imported at THAT time, not the current state. Starting clean
    from "now" means the guard only ever reacts to imports that happen after
    it's watching, which is the only case where the history record and
    current state are guaranteed to still match."""
    hist = await client.get(
        "/api/v3/history", {"page": 1, "pageSize": 1, "sortKey": "date", "sortDirection": "descending"}
    )
    records = hist.get("records", []) if isinstance(hist, dict) else (hist if isinstance(hist, list) else [])
    return int(records[0].get("id", 0)) if records else 0


async def run_quality_guard(settings: Settings, now_ts: float) -> str | None:
    """Deterministic post-import quality re-validation, run alongside the
    other monitor pre-checks (same pattern as run_download_strike_check).
    Returns a report note, or None if nothing needed correcting.

    now_ts is accepted (unused directly, kept for signature symmetry with
    run_download_strike_check and future rate-based logic) so callers don't
    need to special-case this checker."""
    services = build_services(settings)
    is_first_run = not STATE_PATH.exists()
    state = _load_state()
    fixed: list[str] = []
    escalated: list[str] = []

    for client, source in ((services.radarr, "radarr"), (services.sonarr, "sonarr")):
        if client is None:
            continue
        if is_first_run:
            try:
                state["last_history_id"][source] = await _bootstrap_high_water_mark(client)
            except Exception:
                logger.exception("quality guard: failed to bootstrap high-water mark for %s", source)
            continue
        since_id = int(state["last_history_id"].get(source, 0))
        try:
            fresh = await _fetch_new_imports(client, since_id)
        except Exception:
            logger.exception("quality guard: failed to fetch %s import history", source)
            continue
        if not fresh:
            continue
        state["last_history_id"][source] = max(r.get("id", since_id) for r in fresh)

        for record in fresh:
            if rate_limit_exceeded():
                logger.warning("quality guard hit rate limit; deferring remaining checks")
                break
            quality_name = ((record.get("quality") or {}).get("quality") or {}).get("name", "")
            custom_formats = record.get("customFormats") or []
            score = int(record.get("customFormatScore") or 0)
            reason = evaluate_import(quality_name, custom_formats, score)
            if reason is None:
                continue

            entity_id = record.get("movieId") if source == "radarr" else record.get("seriesId")
            title = record.get("sourceTitle") or "unknown"
            if entity_id is None:
                continue

            offense_key = f"{source}:{entity_id}:{title.lower()}"
            offense_count = int(state["offenses"].get(offense_key, 0)) + 1
            state["offenses"][offense_key] = offense_count

            try:
                await _undo_bad_import(services, client, source, entity_id, record)
                record_action(
                    f"{source}_quality_guard",
                    {"entity_id": entity_id, "title": title, "reason": reason, "offense": offense_count},
                    dry_run=False,
                    outcome="ok",
                )
                fixed.append(f'"{title}" ({reason}) — deleted, blocklisted, re-searching')

                if offense_count >= REPEAT_OFFENDER_THRESHOLD:
                    try:
                        blocked = await _block_exact_title(client, source, entity_id, title)
                    except Exception:
                        logger.exception("quality guard: failed to create exact-title block for %r", title)
                        blocked = False
                    if blocked:
                        fixed.append(
                            f'"{title}" has now failed {offense_count} times — added a permanent '
                            "exact-title block so it can't be grabbed again from any indexer."
                        )
            except Exception:
                logger.exception("quality guard: failed to undo bad import for %r", title)
                escalated.append(f'"{title}" ({reason}) — auto-fix failed, needs a manual look')

    _save_state(state)

    notes: list[str] = []
    if fixed:
        notes.append("Quality guard auto-corrected bad imports: " + "; ".join(fixed) + ".")
    if escalated:
        notes.append("Quality guard could not auto-correct: " + "; ".join(escalated) + ". Worth a look.")
    return "\n".join(notes) if notes else None
