from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import secrets
from pathlib import Path
from typing import Any, cast

import httpx

from seedbox_mcp.errors import UpstreamError

# Matches the LogueOS dispatch_listener's TRACE_ID_RE: ^[a-zA-Z0-9_-]{6,128}$
_TRACE_PREFIX = "nasops"


class DispatchClient:
    """Escalation path for the NAS-ops harness — hands an issue off to the
    LogueOS worker dispatch system (the same one behind the operator's
    dispatch-worker skill) when it's beyond what the harness's own tools can
    fix. Same request shape as tools/loop_seed_dispatch.py in
    LogueOS-Orchestrator: write a prompt file into that repo's inbox, HMAC-
    sign the dispatch body, POST it.

    Deliberately cross-repo (writes into LogueOS-Orchestrator's data dir) —
    that inbox path IS the dispatch_listener's contract, not an
    implementation detail seedbox-mcp owns.
    """

    def __init__(self, listener_url: str, hmac_secret: str, prompt_inbox: str) -> None:
        self.listener_url = listener_url.rstrip("/")
        self.hmac_secret = hmac_secret
        self.prompt_inbox = Path(prompt_inbox)

    async def dispatch(
        self,
        *,
        worker: str,
        prompt: str,
        target_repo: str,
        timeout_seconds: int = 1200,
    ) -> dict[str, Any]:
        trace_id = f"{_TRACE_PREFIX}-{secrets.token_hex(8)}"[:128]
        self.prompt_inbox.mkdir(parents=True, exist_ok=True)
        prompt_path = self.prompt_inbox / f"{trace_id}.prompt.json"
        prompt_path.write_text(json.dumps({"prompt": prompt}), encoding="utf-8")

        # prompt_path in the payload is relative to LogueOS-Orchestrator's
        # repo root (two levels up from data/n8n_inbox), matching what the
        # listener expects.
        relative_prompt_path = f"data/n8n_inbox/{prompt_path.name}"
        payload = {
            "schema_version": "v1",
            "trace_id": trace_id,
            "worker": worker,
            "prompt_path": relative_prompt_path,
            "timeout_seconds": timeout_seconds,
            "tool_profile": "standard_worker",
            "target_repo": target_repo,
        }
        body = json.dumps(payload).encode("utf-8")
        sig = _hmac.new(self.hmac_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{self.listener_url}/dispatch",
                    content=body,
                    headers={"Content-Type": "application/json", "X-W4-Hmac": sig},
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "Dispatch listener is unreachable.",
                {"reason": exc.__class__.__name__},
            ) from exc

        try:
            parsed = response.json() if response.content else {}
        except ValueError:
            parsed = {"raw": response.text[:500]}
        if response.status_code not in (200, 202):
            raise UpstreamError(
                "upstream_unreachable",
                "Dispatch listener rejected the request.",
                {"status_code": response.status_code, "body": parsed},
            )
        return cast(dict[str, Any], {"trace_id": trace_id, "http_status": response.status_code, **parsed})
