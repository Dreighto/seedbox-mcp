from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool

logger = logging.getLogger("seedbox_mcp.tools.host_health")

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]

# Emitted on the NAS to collect live resource pressure as one JSON line —
# reads /proc directly (no external deps) plus ps for the top consumers. Uses
# only double quotes so shlex.quote can single-quote it whole for SSH.
_RES_SCRIPT = (
    "import json,os,subprocess\n"
    "def rf(p):\n return open(p).read()\n"
    "la=rf(\"/proc/loadavg\").split()[:3]\n"
    "mem={}\n"
    "for line in rf(\"/proc/meminfo\").splitlines():\n"
    " k,_,v=line.partition(\":\")\n"
    " mem[k]=int(v.split()[0]) if v.split() else 0\n"
    "up=float(rf(\"/proc/uptime\").split()[0])\n"
    "def top(s):\n"
    " o=subprocess.run([\"ps\",\"-eo\",\"pcpu,pmem,comm\",\"--no-headers\",\"--sort=-\"+s],"
    "capture_output=True,text=True).stdout\n"
    " r=[]\n"
    " for l in o.splitlines()[:3]:\n"
    "  p=l.split(None,2)\n"
    "  if len(p)==3: r.append({\"cpu\":float(p[0]),\"mem\":float(p[1]),\"cmd\":p[2]})\n"
    " return r\n"
    "print(json.dumps({\"cores\":os.cpu_count(),\"load\":[float(x) for x in la],"
    "\"mem_total_mb\":mem.get(\"MemTotal\",0)//1024,\"mem_available_mb\":mem.get(\"MemAvailable\",0)//1024,"
    "\"swap_total_mb\":mem.get(\"SwapTotal\",0)//1024,\"swap_free_mb\":mem.get(\"SwapFree\",0)//1024,"
    "\"uptime_s\":up,\"top_cpu\":top(\"pcpu\"),\"top_mem\":top(\"pmem\")}))"
)
_RES_CMD = "python3 -c " + shlex.quote(_RES_SCRIPT)

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


# Services whose logs live at /opt/<svc>/config/logs/<svc>*.txt on the NAS
# (LinuxServer arr/prowlarr layout). Read-only allowlist — the log reader
# can only look at these known application logs, never arbitrary host paths.
_LOG_SERVICES: set[str] = {"radarr", "sonarr", "prowlarr"}


async def nas_log_search(services: Services, service: str, query: str, lines: int = 40) -> dict[str, Any]:
    """Search a media app's own logs on the NAS for a term — the deep-
    diagnosis step when a status/queue signal isn't specific enough (why a
    release was rejected, why a search failed, what an error actually was).
    Read-only: greps the newest few log files for `query` and returns the
    last matching lines. `service` is one of radarr/sonarr/prowlarr."""

    async def run() -> dict[str, Any]:
        if service not in _LOG_SERVICES:
            return ToolResponse.failure(
                "not_permitted", f"{service!r} has no searchable log here.", {"allowed": sorted(_LOG_SERVICES)}
            )
        if not query or not query.strip():
            return ToolResponse.failure("validation", "A non-empty search term is required.")
        n = max(1, min(lines, 200))
        logdir = f"/opt/{service}/config/logs"
        # base64-encode the query locally, decode it remotely, so the
        # untrusted search term never reaches the shell as code (base64 is a
        # shell-safe alphabet). Newest 3 log files, case-insensitive grep,
        # keep the last n matches.
        q_b64 = base64.b64encode(query.encode()).decode()
        cmd = (
            f'q=$(printf %s "{q_b64}" | base64 -d); '
            f"files=$(ls -t {logdir}/{service}*.txt 2>/dev/null | head -3); "
            f'[ -z "$files" ] && echo "__NO_LOGS__" || grep -ih -e "$q" $files | tail -{n}'
        )
        rc, out, err = await _run_on_nas(services, cmd, timeout=30.0)
        if rc != 0 and not out:
            return ToolResponse.failure("nas_unreachable", "Couldn't read the logs over SSH.", {"detail": err[:200]})
        if out.strip() == "__NO_LOGS__":
            return ToolResponse.failure("no_logs", f"No log files found for {service}.")
        matches = [ln for ln in out.splitlines() if ln.strip()]
        return ToolResponse.success(
            {
                "service": service,
                "query": query,
                "match_count": len(matches),
                "lines": matches,
                "note": "Last matching log lines (newest few log files). Empty match_count means the term "
                "didn't appear — the detail may be at a higher log level (Trace) that isn't enabled, or "
                "the event predates the current logs.",
            }
        )

    return await safe_tool(run)


def _summarize_resources(raw: dict[str, Any]) -> dict[str, Any]:
    """Pure. Fold raw /proc + ps data into a resource-pressure rollup with
    deterministic flags (computed here, not left to the model)."""
    cores = raw.get("cores") or 1
    load = raw.get("load") or [0.0, 0.0, 0.0]
    mem_total = raw.get("mem_total_mb", 0)
    mem_avail = raw.get("mem_available_mb", 0)
    mem_used = mem_total - mem_avail
    swap_total = raw.get("swap_total_mb", 0)
    swap_used = swap_total - raw.get("swap_free_mb", 0)
    load_per_core = round(load[0] / cores, 2) if cores else load[0]
    mem_used_pct = round(100 * mem_used / mem_total, 1) if mem_total else 0.0
    flags: list[str] = []
    if load_per_core >= 2.0:
        flags.append("high_load")
    if mem_total and mem_avail / mem_total < 0.08:
        flags.append("memory_pressure")
    if swap_total and swap_used / swap_total > 0.5:
        flags.append("heavy_swap")
    return {
        "cores": cores,
        "load_1m": load[0],
        "load_5m": load[1],
        "load_15m": load[2],
        "load_per_core": load_per_core,
        "mem_total_mb": mem_total,
        "mem_used_mb": mem_used,
        "mem_available_mb": mem_avail,
        "mem_used_pct": mem_used_pct,
        "swap_used_mb": swap_used,
        "uptime_hours": round((raw.get("uptime_s") or 0) / 3600, 1),
        "top_cpu": raw.get("top_cpu", []),
        "top_mem": raw.get("top_mem", []),
        "pressure": flags or ["none"],
        "healthy": not flags,
    }


async def nas_resources(services: Services) -> dict[str, Any]:
    """Live resource pressure on the NAS box: CPU load (1/5/15m + per-core),
    memory used/available, swap, uptime, and the top CPU/memory-consuming
    processes. Deterministic pressure flags (high_load, memory_pressure,
    heavy_swap) are computed in code. Use to answer "is the NAS under load /
    low on memory / what's hogging it" — the live-pressure view that disk
    SMART and free-space checks don't give. Read-only."""

    async def run() -> dict[str, Any]:
        rc, out, err = await _run_on_nas(services, _RES_CMD)
        if rc != 0:
            return ToolResponse.failure("host_unreachable", err.strip() or "SSH to NAS failed.")
        try:
            raw = json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return ToolResponse.failure("parse_error", f"Unexpected output: {out[:200]}")
        return ToolResponse.success(_summarize_resources(raw))

    return await safe_tool(run)
