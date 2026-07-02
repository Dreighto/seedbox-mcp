from __future__ import annotations

import logging
from typing import Any

from seedbox_mcp.download_strikes import classify_queue_item
from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool
from seedbox_mcp.tools.host_health import _run_on_nas

logger = logging.getLogger("seedbox_mcp.import_diagnosis")

# Container name + the arr's library mount points (container-side), used to
# pick which library root an import would land in and to translate a
# container path back to its host path for the remediation command.
_ARR_META: dict[str, dict[str, Any]] = {
    "radarr": {"container": "radarr", "library_mounts": ("/movies", "/anime-movies")},
    "sonarr": {"container": "sonarr", "library_mounts": ("/anime", "/tv")},
}

_REMEDIATION_NOTE = (
    "Filesystem ownership change on the NAS. Run it yourself or escalate; the bot does not auto-run chown."
)


def translate_to_host(container_path: str, mounts: list[tuple[str, str]]) -> str | None:
    """Map a container path to its host path using the container's docker
    mounts (host, container) pairs. Longest container-prefix wins, so
    /downloads/complete beats /downloads."""
    best: tuple[int, str] | None = None
    for host, cont in mounts:
        cont = cont.rstrip("/")
        matches = container_path == cont or container_path.startswith(cont + "/")
        if matches and (best is None or len(cont) > best[0]):
            suffix = container_path[len(cont) :]
            best = (len(cont), host.rstrip("/") + suffix)
    return best[1] if best else None


def _parse_mounts(raw: str) -> list[tuple[str, str]]:
    """docker inspect emits 'src=>dst src=>dst ...' (our -f format). Returns
    (host, container) pairs. Host paths can contain spaces (e.g. 'Anime
    Movies') but container dsts never do, so we split on '=>' and take the
    container dst as everything up to the first space of each chunk."""
    mounts: list[tuple[str, str]] = []
    # raw is a flat run of "HOST=>CONT HOST=>CONT ...". Container dsts never
    # contain spaces, so after splitting on '=>', each middle chunk is
    # "CONT nexthost" and the container dst ends at the last space.
    parts = raw.strip().split("=>")
    # parts[0] = first host; parts[i] = "CONT nexthost"; last = final cont.
    if len(parts) < 2:
        return mounts
    host = parts[0].strip()
    for mid in parts[1:-1]:
        # Container dst is everything up to the FIRST space; the remainder is
        # the next host path (which may itself contain spaces).
        cont, _, next_host = mid.strip().partition(" ")
        mounts.append((host, cont.strip()))
        host = next_host.strip()
    mounts.append((host, parts[-1].strip()))
    return mounts


async def _arr_mounts(services: Services, container: str) -> list[tuple[str, str]]:
    rc, out, _err = await _run_on_nas(
        services,
        f'docker inspect -f "{{{{range .Mounts}}}}{{{{.Source}}}}=>{{{{.Destination}}}} {{{{end}}}}" {container}',
    )
    return _parse_mounts(out) if rc == 0 else []


def _arr_reason(item: dict[str, Any]) -> str:
    """The arr's own words for why an item won't import, from statusMessages
    (+ errorMessage). This is the authoritative reason and must be consulted
    before any filesystem guess."""
    parts = [str(item.get("errorMessage") or "")]
    for sm in item.get("statusMessages") or []:
        if isinstance(sm, dict):
            parts.extend(str(m) for m in (sm.get("messages") or []))
    return " ".join(p for p in parts if p).strip()


async def _diagnose_item(services: Services, source: str, item: dict[str, Any]) -> dict[str, Any]:
    meta = _ARR_META[source]
    container = str(meta["container"])
    output_path = item.get("outputPath") or ""
    title = (item.get("movie") or item.get("series") or {}).get("title") or item.get("title") or "unknown"
    result: dict[str, Any] = {"title": title, "source": source, "output_path": output_path}

    # The arr's OWN reason comes first — it's authoritative and often has
    # nothing to do with the filesystem (a title mismatch, a sample, a
    # not-an-upgrade rejection). Only fall through to the permission/path
    # access check when the message points at the filesystem or is absent.
    reason = _arr_reason(item)
    low = reason.lower()
    result["arr_reason"] = reason or None
    match_markers = ("mismatch", "matched to", "unknown", "not found", "no files found are eligible")
    if any(m in low for m in match_markers):
        result["diagnosis"] = "match_problem"
        result["explanation"] = (
            f'{source} reports: "{reason}". This is a MATCHING problem, not permissions: {source} '
            "can't confidently tie this release to a title in its library (wrong/ambiguous name, the "
            "title isn't added, or a year/edition mismatch). The fix is a manual import / correcting "
            "the match in the arr, not a chown. Do NOT just blocklist-and-redownload; a fresh copy "
            "will hit the same mismatch."
        )
        result["remediation_note"] = "Manual import / match correction in the arr UI (or escalate); not a bot action."
        return result
    if "sample" in low:
        result["diagnosis"] = "sample_file"
        result["explanation"] = (
            f'{source} reports: "{reason}". The download is a sample, not the real file; it will never import.'
        )
        result["remediation_note"] = "Safe to remove+blocklist and re-search for a real release."
        return result
    if "not an upgrade" in low or "already" in low:
        result["diagnosis"] = "not_an_upgrade"
        result["explanation"] = (
            f'{source} reports: "{reason}". The existing file is equal or better, so this copy was '
            "correctly skipped. Benign."
        )
        return result

    if not output_path:
        result["diagnosis"] = "no_output_path"
        result["explanation"] = (
            f'{source} gave no output path and its reason was {reason or "empty"}; too early or too vague '
            "to diagnose. Try nas_log_search for this release name to see the arr's own log detail."
        )
        return result

    # Run the real access tests AS the arr's own UID inside its own
    # container — this is the exact context the import runs in, so the answer
    # isn't inferred from perm bits, it's the actual can-it-touch-the-file
    # result. -u 1000 matches the PUID the media stack runs as (verified).
    lib_checks = " ".join(
        f'if [ -d "{m}" ]; then test -w "{m}" && echo "LIBOK {m}" || echo "LIBNOWRITE {m}"; fi'
        for m in meta["library_mounts"]
    )
    script = (
        f'if [ ! -e "{output_path}" ]; then echo "MISSING"; else '
        f'ls -ld "{output_path}" 2>/dev/null | head -1; '
        f'test -r "{output_path}" && echo "READ_OK" || echo "READ_DENIED"; '
        f'PARENT=$(dirname "{output_path}"); '
        f'test -w "$PARENT" && echo "PARENT_WRITE_OK" || echo "PARENT_WRITE_DENIED"; '
        f"fi; {lib_checks}"
    )
    rc, out, err = await _run_on_nas(services, f"docker exec -u 1000:1000 {container} sh -c '{script}'")
    if rc != 0:
        result["diagnosis"] = "check_failed"
        result["explanation"] = f"Couldn't run the access check in the {container} container."
        result["detail"] = err[:200]
        return result

    lines = out.splitlines()
    result["raw_checks"] = lines
    if "MISSING" in lines:
        host = translate_to_host(output_path, await _arr_mounts(services, container))
        result["diagnosis"] = "path_not_found"
        result["explanation"] = (
            f"The download's output path {output_path!r} does not exist from inside the {container} "
            "container. That's a path-mapping or mount problem, not permissions: the download client "
            "reported a path the arr can't see. Check that the download client and the arr agree on "
            "the completed-downloads path (or that a remote path mapping is applying)."
        )
        result["host_path"] = host
        return result

    read_denied = "READ_DENIED" in lines
    parent_denied = "PARENT_WRITE_DENIED" in lines
    lib_denied = [ln.split(" ", 1)[1] for ln in lines if ln.startswith("LIBNOWRITE")]
    owner_line = next((ln for ln in lines if ln.startswith(("d", "-"))), None)

    if read_denied or parent_denied:
        host = translate_to_host(output_path, await _arr_mounts(services, container))
        result["diagnosis"] = "download_permissions"
        result["explanation"] = (
            f"Permissions problem on the DOWNLOAD side. The {container} process (uid 1000) "
            + ("cannot read the downloaded files" if read_denied else "cannot modify the download folder")
            + f". Current ownership/mode: {owner_line or 'unknown'}. This is the most common import "
            "failure cause."
        )
        if host:
            result["remediation"] = f"chown -R 1000:1000 '{host}' && chmod -R u+rwX,g+rwX '{host}'"
        result["remediation_note"] = _REMEDIATION_NOTE
        return result

    if lib_denied:
        host_libs = []
        mounts = await _arr_mounts(services, container)
        for lib in lib_denied:
            h = translate_to_host(lib, mounts)
            if h:
                host_libs.append(h)
        result["diagnosis"] = "library_permissions"
        result["explanation"] = (
            f"Permissions problem on the LIBRARY side. The {container} process (uid 1000) cannot write "
            f"to its library folder(s): {', '.join(lib_denied)}. The download is fine; the destination "
            "is not writable."
        )
        if host_libs:
            result["remediation"] = " ; ".join(f"chown -R 1000:1000 '{h}'" for h in host_libs)
        result["remediation_note"] = _REMEDIATION_NOTE
        return result

    result["diagnosis"] = "no_permission_or_path_issue_found"
    result["explanation"] = (
        "The arr can read the download and write the library, so this import failure is not a simple "
        "permissions or path problem. Likely candidates: a still-unpacking/_UNPACK_ archive, a "
        "sample/incomplete file, or an unusual naming/qualification mismatch. Next step: nas_log_search "
        f"the {source} log for this release name to see the arr's own detailed reason, then escalate."
    )
    return result


async def nas_import_diagnosis(services: Services) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.settings.nas_ssh_target:
            return ToolResponse.failure("not_configured", "NAS_SSH_TARGET is not set; host access is unavailable.")
        stuck: list[tuple[str, dict[str, Any]]] = []
        for client, source in ((services.radarr, "radarr"), (services.sonarr, "sonarr")):
            if client is None:
                continue
            unknown = "includeUnknownMovieItems" if source == "radarr" else "includeUnknownSeriesItems"
            try:
                q = await client.get("/api/v3/queue", {"page": 1, "pageSize": 200, unknown: "true"})
            except Exception:
                logger.exception("failed to fetch %s queue for import diagnosis", source)
                continue
            records = q.get("records", []) if isinstance(q, dict) else (q if isinstance(q, list) else [])
            for r in records:
                category, _reason = classify_queue_item(r)
                if category == "import_issue":
                    stuck.append((source, r))
        if not stuck:
            return ToolResponse.success(
                {"import_stuck_count": 0, "note": "No downloads are currently stuck on import."}
            )
        diagnoses = [await _diagnose_item(services, source, item) for source, item in stuck]
        return ToolResponse.success({"import_stuck_count": len(diagnoses), "diagnoses": diagnoses})

    return await safe_tool(run)
