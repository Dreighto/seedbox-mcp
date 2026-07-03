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

# Thresholds a fix tool must clear before it's a graduation candidate. These
# are deliberately strict — "graduated to unattended autonomy" is a real trust
# jump, and a handful of lucky identical runs is not "fixed things properly":
# - MIN_SUCCESSES: total clean real executions.
# - MIN_DISTINCT: those successes must span at least this many DISTINCT
#   scenarios (e.g. restarting 5 different services, not tautulli 5x) — proof
#   it handles variety, not one memorised case.
# - RECENCY_DAYS: proof expires; a tool that worked months ago but hasn't been
#   exercised since has to re-earn it, so stale success can't carry a tool that
#   may have since regressed.
# ANY real failure in-window still forces "review" regardless of successes.
GRADUATION_MIN_SUCCESSES = 5
GRADUATION_MIN_DISTINCT = 2
GRADUATION_RECENCY_DAYS = 30.0


@dataclass
class ToolReadiness:
    tool: str
    real_success: int  # all-time clean real executions
    recent_success: int  # clean real executions within the recency window
    distinct_scenarios: int  # distinct arg-signatures among recent successes
    real_fail: int  # real executions that returned a failure/error (in-window)
    blocked: int  # times a safety gate (entity/confirm) stopped the call — the guard working
    dry_run: int
    autonomous: bool
    verdict: str  # autonomous | ready | proving | review | unproven


def _is_failure(outcome: str) -> bool:
    return outcome.startswith("error") or outcome.startswith("failed")


def _scenario_key(args: Any) -> str:
    """Stable signature of WHAT an action targeted, ignoring the confirm/
    dry_run flags — so distinct-scenario counting reflects distinct targets."""
    if not isinstance(args, dict):
        return json.dumps(args, sort_keys=True, default=str)
    trimmed = {k: v for k, v in args.items() if k not in ("confirm", "dry_run")}
    return json.dumps(trimmed, sort_keys=True, default=str)


def _verdict(
    recent_success: int, distinct: int, real_fail: int, autonomous: bool, threshold: int, min_distinct: int
) -> str:
    if autonomous:
        return "autonomous"
    if real_fail > 0:
        # Any real failure means "inspect before trusting it unattended" —
        # don't silently graduate something that has errored in the field.
        return "review"
    if recent_success >= threshold and distinct >= min_distinct:
        return "ready"
    if recent_success > 0:
        # Has clean successes but not yet enough, or not across enough
        # distinct scenarios — still earning it.
        return "proving"
    return "unproven"


def analyze_readiness(
    rows: list[dict[str, Any]],
    action_tools: set[str],
    monitor_tools: set[str],
    now_ts: float,
    threshold: int = GRADUATION_MIN_SUCCESSES,
    min_distinct: int = GRADUATION_MIN_DISTINCT,
    recency_days: float = GRADUATION_RECENCY_DAYS,
) -> list[ToolReadiness]:
    """Pure. Per interactive action tool, tally its audited outcomes and
    classify graduation readiness. `rows` are parsed .action_audit.jsonl
    entries; `monitor_tools` is the current autonomous set. `now_ts` anchors
    the recency window (pass time.time())."""
    cutoff = now_ts - recency_days * 86400
    out: list[ToolReadiness] = []
    for tool in sorted(action_tools | monitor_tools):
        entries = [r for r in rows if r.get("tool") == tool]
        real = [r for r in entries if not r.get("dry_run")]
        real_success = sum(1 for r in real if str(r.get("outcome")) == "ok")
        in_window = [r for r in real if isinstance(r.get("ts"), int | float) and r["ts"] >= cutoff]
        recent_success_rows = [r for r in in_window if str(r.get("outcome")) == "ok"]
        distinct = len({_scenario_key(r.get("args")) for r in recent_success_rows})
        real_fail = sum(1 for r in in_window if _is_failure(str(r.get("outcome", ""))))
        blocked = sum(1 for r in entries if str(r.get("outcome", "")).startswith("blocked"))
        autonomous = tool in monitor_tools
        out.append(
            ToolReadiness(
                tool=tool,
                real_success=real_success,
                recent_success=len(recent_success_rows),
                distinct_scenarios=distinct,
                real_fail=real_fail,
                blocked=blocked,
                dry_run=sum(1 for r in entries if r.get("dry_run")),
                autonomous=autonomous,
                verdict=_verdict(
                    len(recent_success_rows), distinct, real_fail, autonomous, threshold, min_distinct
                ),
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
        return (
            f"  {r.tool:30s} recent_ok={r.recent_success} distinct={r.distinct_scenarios} "
            f"fail={r.real_fail} (all-time ok={r.real_success})"
        )

    if groups["ready"]:
        lines.append(f"READY TO GRADUATE (>={threshold} recent clean, >=2 distinct, 0 fails):")
        lines += [row(r) for r in groups["ready"]]
        lines.append("")
    if groups["review"]:
        lines.append("NEEDS REVIEW (has real failures in-window — inspect before graduating):")
        lines += [row(r) for r in groups["review"]]
        lines.append("")
    if groups["proving"]:
        lines.append(f"PROVING (recent clean successes, below {threshold} or too few distinct):")
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


def build_nudge(readiness: list[ToolReadiness]) -> str | None:
    """Short, Telegram-safe nudge for the daily digest — only when there's
    something actionable. Returns None (append nothing) on a quiet day, so the
    digest isn't cluttered with "nothing to graduate" noise every run."""
    ready = [r for r in readiness if r.verdict == "ready"]
    review = [r for r in readiness if r.verdict == "review"]
    if not ready and not review:
        return None
    lines = ["*Monitor autonomy*"]
    for r in ready:
        lines.append(
            f"{r.tool} has proven itself ({r.recent_success} recent clean fixes across "
            f"{r.distinct_scenarios} scenarios, 0 failures). Consider graduating it to the "
            "unattended monitor."
        )
    for r in review:
        n = r.real_fail
        lines.append(
            f"{r.tool} has {n} real failure{'s' if n != 1 else ''} on record. "
            "Inspect before trusting it unattended."
        )
    return "\n".join(lines)


def graduation_nudge() -> str | None:
    """Load the real audit + tool sets and build the digest nudge, or None."""
    import time

    from seedbox_mcp.action_audit import AUDIT_LOG_PATH
    from seedbox_mcp.chat.ollama_ai import ACTION_TOOLS
    from seedbox_mcp.monitor import MONITOR_ACTION_TOOLS, MONITOR_DETERMINISTIC_TOOLS

    rows = load_audit_rows(AUDIT_LOG_PATH)
    autonomous = MONITOR_ACTION_TOOLS | MONITOR_DETERMINISTIC_TOOLS
    return build_nudge(analyze_readiness(rows, ACTION_TOOLS, autonomous, time.time()))


def main() -> None:
    import time

    from seedbox_mcp.action_audit import AUDIT_LOG_PATH
    from seedbox_mcp.chat.ollama_ai import ACTION_TOOLS
    from seedbox_mcp.monitor import MONITOR_ACTION_TOOLS, MONITOR_DETERMINISTIC_TOOLS

    rows = load_audit_rows(AUDIT_LOG_PATH)
    autonomous = MONITOR_ACTION_TOOLS | MONITOR_DETERMINISTIC_TOOLS
    readiness = analyze_readiness(rows, ACTION_TOOLS, autonomous, time.time())
    print(format_report(readiness))


if __name__ == "__main__":
    main()
