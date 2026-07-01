from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool

# claude-code has full shell/SSH access and system-level judgment — that's
# the "bigger model with more capabilities" the operator asked for, worth
# the real cost for genuine escalation (unlike routine work, which the
# operator's standing directive routes to the flat-rate Ollama-Cloud aider
# family — dpsk/ki/glm — instead). Override only for something that's
# genuinely a well-scoped code change an aider worker could handle alone.
DEFAULT_ESCALATION_WORKER = "claude-code"
DEFAULT_TARGET_REPO = "nas"

ESCALATION_PROMPT_TEMPLATE = """\
You're being dispatched by the NAS Ops Telegram bot (@nas_doombot) — an \
Ollama-Cloud agent running on ROOM with read-only + Tier-1-reversible tools \
against seedbox-mcp (~/dev/seedbox-mcp) and the NASDOOM BFF. It found \
something during a status check or a live conversation with the operator \
that its own tools can't fix, and is escalating to you.

## What it found

{issue}

## What it needs from you

Investigate and fix it if you can, using whatever access you need (SSH into \
the NAS/Jetson/ROOM services, edit systemd units, restart services, etc. — \
you are not scoped to the "nas" repo, that's just the dispatch anchor). If \
you fix it, verify the fix actually works before reporting done — don't \
just edit a config and assume. If it needs the operator's judgment call \
(a real decision, not just execution), report back what you found and what \
you'd recommend rather than guessing.

This ticket was not filed by the operator directly — they haven't seen this \
issue yet, so explain it clearly rather than assuming context.
"""


async def escalate_to_worker(
    services: Services,
    issue: str,
    worker: str = DEFAULT_ESCALATION_WORKER,
    target_repo: str = DEFAULT_TARGET_REPO,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.dispatch:
            return ToolResponse.failure("dispatch_unavailable", "Worker dispatch is not configured.")
        prompt = ESCALATION_PROMPT_TEMPLATE.format(issue=issue)
        result = await services.dispatch.dispatch(worker=worker, prompt=prompt, target_repo=target_repo)
        return ToolResponse.success(
            {
                "escalated": True,
                "worker": worker,
                "trace_id": result.get("trace_id"),
                "listener_response": result,
            }
        )

    return await safe_tool(run)
