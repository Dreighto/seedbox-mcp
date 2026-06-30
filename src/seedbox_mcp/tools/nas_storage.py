from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool

# Backup units this tool is allowed to inspect via read-only `systemctl show`.
# Cadence is informational only — used to decide when a unit counts as stale.
# This is a HEALTH CHECK, not a trigger: nothing here starts/stops/restarts
# anything, so it needs no privilege beyond reading systemd unit state.
RESTIC_UNITS: dict[str, dict[str, Any]] = {
    "local_daily": {"unit": "room-restic-backup.service", "stale_after_hours": 30},
    "nas_offsite": {"unit": "room-restic-nas-copy.service", "stale_after_hours": 30},
    "ssd_emergency": {"unit": "room-restic-ssd-copy.service", "stale_after_hours": 192},
}

# Directories this tool may inventory, by label. Root-confined to this exact
# allowlist — there is no path parameter that reaches the filesystem, so a
# model can never walk this tool outside the configured set. Read-only: stat
# and `du` only, file contents are never opened.
WATCHED_DIRS: dict[str, Path] = {
    "music": Path("/mnt/nas-pool/Music"),
    "samples": Path("/mnt/nas-pool/samples"),
    "transfer": Path("/mnt/nas-pool/Transfer"),
}


async def nas_backup_health() -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        results: dict[str, Any] = {}
        now = datetime.now(UTC)
        for label, spec in RESTIC_UNITS.items():
            unit = spec["unit"]
            stale_after = spec["stale_after_hours"]
            props = await _systemctl_show(
                unit, ["ActiveState", "Result", "ExecMainStatus", "ExecMainExitTimestamp"]
            )
            exit_ts = _parse_systemd_timestamp(props.get("ExecMainExitTimestamp"))
            hours_since = (now - exit_ts).total_seconds() / 3600 if exit_ts else None
            result = props.get("Result") or "unknown"
            if exit_ts is None:
                status = "never_run"
            elif result != "success":
                status = "failed"
            elif hours_since is not None and hours_since > stale_after:
                status = "stale"
            else:
                status = "ok"
            results[label] = {
                "unit": unit,
                "status": status,
                "last_result": result,
                "last_exit_at": exit_ts.isoformat() if exit_ts else None,
                "hours_since_last_run": round(hours_since, 1) if hours_since is not None else None,
                "stale_after_hours": stale_after,
            }
        overall_ok = all(r["status"] == "ok" for r in results.values())
        return ToolResponse.success({"overall_ok": overall_ok, "backups": results})

    return await safe_tool(run)


async def nas_storage_inventory(labels: list[str] | None = None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        wanted = labels or list(WATCHED_DIRS.keys())
        unknown = [label for label in wanted if label not in WATCHED_DIRS]
        warnings = [f"unknown watched-dir label: {label}" for label in unknown]
        data: dict[str, Any] = {}
        for label in wanted:
            path = WATCHED_DIRS.get(label)
            if path is None:
                continue
            data[label] = await _inventory_one(path)
        return ToolResponse.success({"watched_dirs": data}, warnings)

    return await safe_tool(run)


async def _inventory_one(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        proc = await asyncio.create_subprocess_exec(
            "du",
            "-sb",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        size_bytes = int(stdout.split()[0]) if stdout else None
    except (TimeoutError, ValueError, IndexError):
        size_bytes = None

    entries = list(path.iterdir()) if path.is_dir() else []
    newest_mtime = max((e.stat().st_mtime for e in entries), default=None) if entries else None
    return {
        "path": str(path),
        "exists": True,
        "size_gb": round(size_bytes / 1024**3, 2) if size_bytes is not None else None,
        "top_level_entry_count": len(entries),
        "newest_entry_modified_at": (
            datetime.fromtimestamp(newest_mtime, tz=UTC).isoformat() if newest_mtime else None
        ),
    }


async def _systemctl_show(unit: str, properties: list[str]) -> dict[str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "show",
            unit,
            f"--property={','.join(properties)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:
        return {}
    result: dict[str, str] = {}
    for line in stdout.decode().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key] = value
    return result


def _parse_systemd_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    # systemd format: "Tue 2026-06-30 04:34:52 PDT" — strip the weekday and
    # tz abbreviation (not reliably parseable) and assume local time, which
    # is fine for a "how many hours ago" calc on the box that produced it.
    parts = value.split()
    if len(parts) < 3:
        return None
    try:
        naive = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.astimezone(UTC)
