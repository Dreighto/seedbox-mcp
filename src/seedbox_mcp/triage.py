from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from html import escape
from pathlib import Path

logger = logging.getLogger("seedbox_mcp.triage")

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
    """Truncated to 40 chars — this id ends up inside a Telegram callback_data
    string (`dgf:{run_id}:{finding_id}:{action}`), which has a hard 64-byte
    limit. A long finding title ("Request 'Disclosure Day' stuck in
    processing for 9 days past release") would blow that limit untruncated."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "finding")[:40].rstrip("-")


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
    return findings


_ACTIONABLE = ("needs_fix", "watch")
_GROUP_LABELS = {"needs_fix": "NEEDS FIX", "watch": "WATCH"}
_GROUP_ICON = {"needs_fix": "\U0001f534", "watch": "\U0001f7e1"}  # red / yellow circle


# ── run persistence (so a button tap, which arrives later, can resolve back
# to the full finding it was rendered from) ──────────────────────────────
_RUNS_DIR = Path(__file__).resolve().parent.parent.parent / ".digest_runs"
_MAX_STORED_RUNS = 20


def _runs_dir() -> Path:
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return _RUNS_DIR


def save_run(findings: list[Finding]) -> str:
    """Persist a findings list under a short run id and return it. Prunes to
    the most recent _MAX_STORED_RUNS files so this never grows unbounded —
    a digest run is actionable for a few hours at most, not forever."""
    run_id = uuid.uuid4().hex[:8]
    path = _runs_dir() / f"{run_id}.json"
    payload = [
        {
            "id": f.id,
            "severity": f.severity,
            "title": f.title,
            "real": f.real,
            "reason": f.reason,
            "recommendation": f.recommendation,
            "fixable_by": f.fixable_by,
            "evidence": f.evidence,
            "auto_fixed": f.auto_fixed,
        }
        for f in findings
    ]
    path.write_text(json.dumps(payload))
    _prune_old_runs()
    return run_id


def _prune_old_runs() -> None:
    try:
        files = sorted(_runs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[_MAX_STORED_RUNS:]:
            stale.unlink(missing_ok=True)
    except OSError:
        logger.exception("failed to prune old digest runs")


def load_run(run_id: str) -> list[Finding] | None:
    path = _runs_dir() / f"{run_id}.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return [Finding(**item) for item in raw]


def load_finding(run_id: str, finding_id: str) -> Finding | None:
    findings = load_run(run_id)
    if findings is None:
        return None
    return next((f for f in findings if f.id == finding_id), None)


# ── rendering ────────────────────────────────────────────────────────────


def fingerprint(findings: list[Finding]) -> str | None:
    keys = sorted(
        f"{f.severity}:{f.title}"
        for f in findings
        if f.severity in _ACTIONABLE and not f.auto_fixed
    )
    if not keys:
        return None
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def _title_line(f: Finding) -> str:
    return f"{_GROUP_ICON.get(f.severity, '•')} {escape(f.title)}"


def render_triage(findings: list[Finding], *, run_id: str | None = None) -> tuple[str, dict | None]:
    """Renders a CEO-brief: a title line per actionable finding, nothing
    else in the body. Full reason/recommendation/evidence lives behind a
    "More" button (see load_finding) instead of being dumped inline — this
    is the fix for the report reading as a wall of text. Auto-fixed items
    get one past-tense line each. Healthy checks collapse to a single
    line, never enumerated by name, per the standing "if nothing needs
    attention, don't explain why" rule.

    `run_id` must be the id `save_run()` returned for THIS findings list —
    pass it to get real Yes/No/More buttons; omit it (or pass None) to get
    plain text with no keyboard, e.g. for a one-off on-demand status check
    that has nowhere to route a button tap back to."""
    active = [f for f in findings if not f.auto_fixed]
    auto = [f for f in findings if f.auto_fixed]
    attention = [f for f in active if f.severity in _ACTIONABLE]

    header = f"<b>NAS OPS, {len(attention)} need your attention</b>" if attention else "<b>NAS OPS, all clear</b>"
    blocks = [header]
    keyboard_rows: list[list[dict]] = []

    for sev in _ACTIONABLE:
        group = [f for f in active if f.severity == sev]
        if not group:
            continue
        blocks.append("\n".join(_title_line(f) for f in group))
        if run_id:
            for f in group:
                keyboard_rows.append(_finding_buttons(run_id, f))

    if auto:
        body = "\n".join(f"✅ {escape(f.title)}: {escape(f.reason)}" for f in auto)
        blocks.append(body)

    healthy = [f for f in active if f.severity == "healthy"]
    if healthy:
        blocks.append(f"Everything else normal ({len(healthy)} checked).")

    text = "\n\n".join(blocks)
    markup = {"inline_keyboard": keyboard_rows} if keyboard_rows else None
    return text, markup


def _finding_buttons(run_id: str, f: Finding) -> list[dict]:
    row = []
    if f.fixable_by in ("proven", "tap"):
        row.append({"text": "✅ Fix it", "callback_data": f"dgf:{run_id}:{f.id}:fix"})
    elif f.fixable_by == "agent":
        row.append({"text": "\U0001f527 Escalate", "callback_data": f"dgf:{run_id}:{f.id}:esc"})
    row.append({"text": "ℹ️ More", "callback_data": f"dgf:{run_id}:{f.id}:more"})
    return row


def render_finding_detail(f: Finding) -> str:
    """The full detail behind a finding's "More" button — everything the old
    always-inline render used to dump into every message body."""
    lines = [f"<b>{escape(f.title)}</b>"]
    if f.reason:
        lines.append(escape(f.reason))
    if f.recommendation:
        lines.append(f"<i>Recommend:</i> {escape(f.recommendation)}")
    if f.evidence:
        lines.append(f"<code>{escape(f.evidence)}</code>")
    return "\n\n".join(lines)
