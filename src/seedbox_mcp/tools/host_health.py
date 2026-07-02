from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool

logger = logging.getLogger("seedbox_mcp.tools.host_health")

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]

# Hard allowlist of restartable containers on the NAS — media stack only.
# n8n, ollama, and cloudflared deliberately excluded: they're shared
# infrastructure other systems depend on, restarting them is
# escalate_to_worker territory, not a Tier-1 bot action. The allowlist is
# enforced in code, so a hallucinated or mistyped name fails validation
# before anything runs.
RESTARTABLE_SERVICES: set[str] = {
    "plex",
    "radarr",
    "sonarr",
    "prowlarr",
    "sabnzbd",
    "jellyseerr",
    "tautulli",
    "kometa",
    "recyclarr",
    "filebrowser",
}

# SMART attribute thresholds, Backblaze-drive-stats style (337k+ drives):
# a raw pass/fail hides drives that are statistically about to die. Each
# entry: (attribute id, name, watch threshold, replace threshold). Raw
# value at/above "replace" = replace_now; at/above "watch" = watch.
# Verdicts are computed HERE, in code — the model reports them, it never
# gets to judge disk health itself.
_ATA_THRESHOLDS: list[tuple[int, str, int, int]] = [
    (5, "reallocated_sectors", 1, 100),
    (187, "reported_uncorrectable", 1, 10),
    (188, "command_timeout", 1, 13000),
    (197, "pending_sectors", 1, 10),
    (198, "offline_uncorrectable", 1, 10),
    (199, "udma_crc_errors", 1, 100),
]


async def _run(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _run_on_nas(services: Services, command: str, timeout: float = 30.0) -> tuple[int, str, str]:
    target = services.settings.nas_ssh_target
    if not target:
        return 1, "", "NAS_SSH_TARGET not configured"
    return await _run(["ssh", *SSH_OPTS, target, command], timeout=timeout)


def _judge_ata(attrs: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Deterministic verdict from an ata_smart_attributes table."""
    raw_by_id = {a.get("id"): (a.get("raw") or {}).get("value", 0) for a in attrs}
    reasons: list[str] = []
    verdict = "ok"
    for attr_id, name, watch, replace in _ATA_THRESHOLDS:
        raw = raw_by_id.get(attr_id)
        if not isinstance(raw, int) or raw < watch:
            continue
        # Attribute 188's raw value packs three 16-bit counters; only the
        # low word is the lifetime count.
        if attr_id == 188:
            raw = raw & 0xFFFF
            if raw < watch:
                continue
        if raw >= replace:
            verdict = "replace_now"
            reasons.append(f"{name}={raw} (at/above replace threshold {replace})")
        else:
            if verdict == "ok":
                verdict = "watch"
            reasons.append(f"{name}={raw} (nonzero, worth watching)")
    return verdict, reasons


def _judge_nvme(log: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    verdict = "ok"
    if log.get("critical_warning", 0):
        verdict = "replace_now"
        reasons.append(f"critical_warning={log['critical_warning']}")
    if (used := log.get("percentage_used", 0)) >= 90:
        verdict = "replace_now" if used >= 100 else ("watch" if verdict == "ok" else verdict)
        reasons.append(f"percentage_used={used}%")
    if (media := log.get("media_errors", 0)) > 0:
        if verdict == "ok":
            verdict = "watch"
        reasons.append(f"media_errors={media}")
    return verdict, reasons


def _summarize_device(report: dict[str, Any]) -> dict[str, Any]:
    device = (report.get("device") or {}).get("name", "unknown")
    model = report.get("model_name") or report.get("model_family") or "unknown"
    smart_passed = (report.get("smart_status") or {}).get("passed")
    if "nvme_smart_health_information_log" in report:
        verdict, reasons = _judge_nvme(report["nvme_smart_health_information_log"])
        temp = report["nvme_smart_health_information_log"].get("temperature")
    else:
        table = ((report.get("ata_smart_attributes") or {}).get("table")) or []
        verdict, reasons = _judge_ata(table)
        temp = (report.get("temperature") or {}).get("current")
    # smartctl's own FAILED always wins, whatever the attribute math said.
    if smart_passed is False:
        verdict = "replace_now"
        reasons.insert(0, "smartctl overall self-assessment: FAILED")
    return {
        "device": device,
        "model": model,
        "smart_self_assessment": "PASSED" if smart_passed else ("FAILED" if smart_passed is False else "unknown"),
        "verdict": verdict,
        "reasons": reasons or ["no concerning attributes"],
        "temperature_c": temp,
        "power_on_hours": (report.get("power_on_time") or {}).get("hours"),
    }


async def nas_disk_health(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        rc, out, err = await _run_on_nas(services, "sudo -n smartctl --scan | awk '{print $1}'")
        if rc != 0:
            return ToolResponse.failure("nas_unreachable", "Couldn't reach the NAS over SSH.", {"detail": err[:300]})
        devices = [d.strip() for d in out.splitlines() if d.strip().startswith("/dev/")]
        if not devices:
            return ToolResponse.failure("no_devices", "smartctl found no disks on the NAS.")
        loop = "; ".join(f"echo '===DEV==='; sudo -n smartctl -i -H -A -j {d}" for d in devices)
        rc, out, err = await _run_on_nas(services, loop, timeout=60.0)
        disks = []
        for chunk in out.split("===DEV===")[1:]:
            try:
                disks.append(_summarize_device(json.loads(chunk)))
            except json.JSONDecodeError:
                logger.warning("unparseable smartctl output chunk (%d chars)", len(chunk))
        worst = "ok"
        for d in disks:
            if d["verdict"] == "replace_now":
                worst = "replace_now"
                break
            if d["verdict"] == "watch":
                worst = "watch"
        return ToolResponse.success(
            {
                "overall": worst,
                "disks": disks,
                "note": "Verdicts are computed from Backblaze-style attribute thresholds in code, "
                "not judgment. ~23% of failing drives show no SMART warning at all, so 'ok' means "
                "'no known indicator', not a guarantee.",
            }
        )

    return await safe_tool(run)


async def nas_service_status(services: Services, name: str | None = None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        rc, out, err = await _run_on_nas(services, "docker ps -a --format '{{.Names}}\t{{.State}}\t{{.Status}}'")
        if rc != 0:
            return ToolResponse.failure("nas_unreachable", "Couldn't reach the NAS over SSH.", {"detail": err[:300]})
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and (name is None or parts[0] == name):
                rows.append({"name": parts[0], "state": parts[1], "status": parts[2]})
        if name and not rows:
            return ToolResponse.failure("not_found", f"No container named {name!r} on the NAS.")
        return ToolResponse.success({"services": rows})

    return await safe_tool(run)


async def nas_service_restart(services: Services, name: str, confirm: bool = False) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if name not in RESTARTABLE_SERVICES:
            return ToolResponse.failure(
                "not_permitted",
                f"{name!r} is not a restartable service.",
                {"allowed": sorted(RESTARTABLE_SERVICES)},
            )
        rc, out, err = await _run_on_nas(services, f"docker ps -a --format '{{{{.State}}}}' --filter name=^{name}$")
        if rc != 0:
            return ToolResponse.failure("nas_unreachable", "Couldn't reach the NAS over SSH.", {"detail": err[:300]})
        state_before = out.strip() or "not found"
        if state_before == "not found":
            return ToolResponse.failure("not_found", f"No container named {name!r} on the NAS.")
        if not confirm:
            return ToolResponse.success(
                {"dry_run": True, "would_restart": name, "state_before": state_before}
            )
        rc, _out, err = await _run_on_nas(services, f"docker restart {name}", timeout=90.0)
        if rc != 0:
            return ToolResponse.failure("restart_failed", f"docker restart {name} failed.", {"detail": err[:300]})
        # Verify the outcome in code and report the REAL post-restart state —
        # the model reports this result, it doesn't get to claim success.
        await asyncio.sleep(3)
        rc, out, _err = await _run_on_nas(services, f"docker ps -a --format '{{{{.State}}}}' --filter name=^{name}$")
        state_after = out.strip() or "unknown"
        return ToolResponse.success(
            {
                "dry_run": False,
                "restarted": name,
                "state_before": state_before,
                "state_after": state_after,
                "verified_running": state_after == "running",
            }
        )

    return await safe_tool(run)
