"""Graduation-readiness analyzer for the monitor's autonomous action set.

The operator's rule for the unattended monitor (2026-07-01): a fix tool only
graduates from interactive-only into the monitor's autonomous set once it has
"proven it found issues that actually matter, and fixed them properly." This
module turns that rule into a data gate instead of a gut call: it reads the
append-only action audit (.action_audit.jsonl) and, per fix tool, reports how
many real successful human-driven executions it has, whether it's ever failed
for real, and whether it's already autonomous — then gives a plain verdict.

Read-only. It recommends; it never flips a tool into the autonomous set on its
own (that stays a deliberate operator decision).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Real successful executions (zero real failures) a fix tool must accumulate
# before it's a graduation candidate. Deliberately conservative — a couple of
# lucky runs is not "fixed things properly." Bump per-tool judgement, not this
# global, if a specific tool warrants a higher bar.
GRADUATION_MIN_SUCCESSES = 5


@dataclass
class ToolReadiness:
    tool: str
    real_success: int
    real_fail: int
    blocked: int  # times a safety gate (entity/confirm) stopped the call — the guard working
    dry_run: int
    autonomous: bool
    verdict: str  # autonomous | ready | proving | review | unproven


def _verdict(real_success: int, real_fail: int, autonomous: bool, threshold: int) -> str:
    if autonomous:
        return "autonomous"
    if real_fail > 0:
        # Any real failure means "inspect before trusting it unattended" —
        # don't silently graduate something that has errored in the field.
        return "review"
    if real_success >= threshold:
        return "ready"
    if real_success > 0:
        return "proving"
    return "unproven"


def analyze_readiness(
    rows: list[dict[str, Any]],
    action_tools: set[str],
    monitor_tools: set[str],
    threshold: int = GRADUATION_MIN_SUCCESSES,
) -> list[ToolReadiness]:
    """Pure. Per interactive action tool, tally its audited outcomes and
    classify graduation readiness. `rows` are parsed .action_audit.jsonl
    entries; `monitor_tools` is the current autonomous set."""
    out: list[ToolReadiness] = []
    for tool in sorted(action_tools | monitor_tools):
        entries = [r for r in rows if r.get("tool") == tool]
        real = [r for r in entries if not r.get("dry_run")]
        dry = [r for r in entries if r.get("dry_run")]
        real_success = sum(1 for r in real if str(r.get("outcome")) == "ok")
        real_fail = sum(1 for r in real if str(r.get("outcome", "")).startswith("error"))
        blocked = sum(1 for r in entries if str(r.get("outcome", "")).startswith("blocked"))
        autonomous = tool in monitor_tools
        out.append(
            ToolReadiness(
                tool=tool,
                real_success=real_success,
                real_fail=real_fail,
                blocked=blocked,
                dry_run=len(dry),
                autonomous=autonomous,
                verdict=_verdict(real_success, real_fail, autonomous, threshold),
            )
        )
    return out


def load_audit_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def format_report(readiness: list[ToolReadiness], threshold: int = GRADUATION_MIN_SUCCESSES) -> str:
    groups: dict[str, list[ToolReadiness]] = {
        k: [] for k in ("ready", "review", "proving", "autonomous", "unproven")
    }
    for r in readiness:
        groups[r.verdict].append(r)
    lines: list[str] = []
    lines.append(f"Monitor autonomy graduation readiness (threshold: {threshold} clean successes)")
    lines.append("")

    def row(r: ToolReadiness) -> str:
        return f"  {r.tool:30s} ok={r.real_success} fail={r.real_fail} blocked={r.blocked}"

    if groups["ready"]:
        lines.append("READY TO GRADUATE (proven, zero failures, not yet autonomous):")
        lines += [row(r) for r in groups["ready"]]
        lines.append("")
    if groups["review"]:
        lines.append("NEEDS REVIEW (has real failures — inspect before graduating):")
        lines += [row(r) for r in groups["review"]]
        lines.append("")
    if groups["proving"]:
        lines.append(f"PROVING (some real successes, below {threshold}):")
        lines += [row(r) for r in groups["proving"]]
        lines.append("")
    if groups["autonomous"]:
        lines.append("ALREADY AUTONOMOUS (in the monitor's unattended set):")
        lines += [row(r) for r in groups["autonomous"]]
        lines.append("")
    if groups["unproven"]:
        lines.append("UNPROVEN (no real executions yet — interactive-only):")
        lines += [row(r) for r in groups["unproven"]]
    return "\n".join(lines)


def main() -> None:
    from seedbox_mcp.action_audit import AUDIT_LOG_PATH
    from seedbox_mcp.chat.ollama_ai import ACTION_TOOLS
    from seedbox_mcp.monitor import MONITOR_ACTION_TOOLS

    rows = load_audit_rows(AUDIT_LOG_PATH)
    readiness = analyze_readiness(rows, ACTION_TOOLS, MONITOR_ACTION_TOOLS)
    print(format_report(readiness))


if __name__ == "__main__":
    main()
