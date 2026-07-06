from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html import escape

SEVERITIES = ("needs_fix", "watch", "healthy")
FIXABLE_BY = ("proven", "tap", "agent", "none")


@dataclass
class Finding:
    id: str
    severity: str
    title: str
    real: bool
    reason: str
    recommendation: str = ""
    fixable_by: str = "none"
    evidence: str = ""
    auto_fixed: bool = False


FINDINGS_INSTRUCTION = (
    "After you finish the checks, output ONLY a JSON array of findings and nothing "
    "else, no prose before or after. One object per thing you checked. Each object: "
    '"severity" (one of "needs_fix", "watch", "healthy"), "title" (one short line), '
    '"real" (true if it is a genuine problem, false if it is a false alarm), "reason" '
    '(why, one sentence), "recommendation" (what should happen, empty for healthy), '
    '"fixable_by" (one of "proven", "tap", "agent", "none"), "evidence" (IDs, paths, '
    "or quoted numbers, may be empty). Report every check as its own finding, healthy "
    "ones included. Do not wrap the array in markdown."
)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "finding"


def _extract_array(text: str) -> list | None:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        loaded = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, list) else None


def _valid_item(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("severity") not in SEVERITIES:
        return False
    if not isinstance(item.get("title"), str) or not item["title"].strip():
        return False
    if not isinstance(item.get("real"), bool):
        return False
    if not isinstance(item.get("reason"), str):
        return False
    fb = item.get("fixable_by", "none")
    return fb in FIXABLE_BY


def _fallback(text: str) -> list[Finding]:
    return [
        Finding(
            id="unstructured-cycle-output",
            severity="needs_fix",
            title="Check cycle output could not be structured",
            real=True,
            reason="the model returned output that was not a valid findings array, data unstructured",
            recommendation="read the raw output below and re-run the cycle",
            fixable_by="none",
            evidence=text.strip()[:500],
        )
    ]


def parse_findings(text: str) -> list[Finding]:
    array = _extract_array(text)
    if array is None:
        return _fallback(text)
    findings: list[Finding] = []
    seen: dict[str, int] = {}
    for item in array:
        if not _valid_item(item):
            continue
        base = slugify(item["title"])
        seen[base] = seen.get(base, 0) + 1
        fid = base if seen[base] == 1 else f"{base}-{seen[base]}"
        findings.append(
            Finding(
                id=fid,
                severity=item["severity"],
                title=item["title"].strip(),
                real=item["real"],
                reason=item.get("reason", "").strip(),
                recommendation=item.get("recommendation", "").strip(),
                fixable_by=item.get("fixable_by", "none"),
                evidence=str(item.get("evidence", "")).strip(),
            )
        )
    return findings or _fallback(text)


_ACTIONABLE = ("needs_fix", "watch")
_GROUP_LABELS = {"needs_fix": "NEEDS FIX", "watch": "WATCH"}


def fingerprint(findings: list[Finding]) -> str | None:
    keys = sorted(
        f"{f.severity}:{f.title}"
        for f in findings
        if f.severity in _ACTIONABLE and not f.auto_fixed
    )
    if not keys:
        return None
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def _render_finding(f: Finding) -> str:
    lines = [f"- {escape(f.title)}"]
    detail = f.reason
    if f.recommendation:
        detail = f"{detail} recommend: {f.recommendation}" if detail else f"recommend: {f.recommendation}"
    if detail:
        lines.append(f"  {escape(detail)}")
    if f.evidence:
        lines.append(f"  <code>{escape(f.evidence)}</code>")
    return "\n".join(lines)


def render_triage(findings: list[Finding], *, interactive: bool = False) -> tuple[str, dict | None]:
    active = [f for f in findings if not f.auto_fixed]
    auto = [f for f in findings if f.auto_fixed]
    attention = [f for f in active if f.severity in _ACTIONABLE]

    header = f"NAS OPS, {len(attention)} need attention" if attention else "NAS OPS, all healthy"
    blocks = [f"<b>{escape(header)}</b>"]

    for sev in _ACTIONABLE:
        group = [f for f in active if f.severity == sev]
        if group:
            blocks.append(f"<b>{_GROUP_LABELS[sev]} ({len(group)})</b>\n" + "\n".join(_render_finding(f) for f in group))

    if auto:
        body = "\n".join(f"- {escape(f.title)}: {escape(f.reason)}" for f in auto)
        blocks.append(f"<b>AUTO-FIXED THIS CYCLE ({len(auto)})</b>\n" + body)

    healthy = [f for f in active if f.severity == "healthy"]
    if healthy:
        names = ", ".join(escape(f.title) for f in healthy)
        blocks.append(f"<b>HEALTHY ({len(healthy)})</b>: {names}")

    return "\n\n".join(blocks), None
