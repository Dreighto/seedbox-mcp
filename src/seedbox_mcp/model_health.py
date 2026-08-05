from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable

import httpx

from seedbox_mcp.chat.ollama_ai import KEEP_ALIVE
from seedbox_mcp.model_registry import CloudModel

# Generous on purpose: a model with no recent traffic (e.g. qwen3.5:397b-cloud,
# only used for the rare photo-identify path) is essentially always cold
# between 30-min monitor cycles, and this codebase already has a documented,
# live-confirmed fact about exactly that — Ollama Cloud calls that omit
# keep_alive see ~4-5 min cold-start latency after an idle gap (see
# ollama_ai.py's own KEEP_ALIVE comment). A 30s timeout mistook that for a
# dead model twice in one day (2026-08-05, 04:07 and 13:06) — both were
# ollama ps showing zero resident models at the time, not a real 410/retired
# case. This timeout has to sit comfortably above that cold-start window, not
# just above a normal warm response.
_PING_TIMEOUT_S = 300.0

# A single failed ping isn't proof of a real outage. Same "don't page on one
# blip" principle the rest of monitor.py already applies to service
# reachability — a genuinely retired/broken model (like
# qwen3-coder:480b-cloud's 410) fails identically on a retry; a cold-start
# timeout usually doesn't, since the first attempt's request already kicked
# off the load.
_RETRY_DELAY_S = 5.0


async def check_model(ollama_url: str, model: str, timeout: float = _PING_TIMEOUT_S) -> str | None:
    """Pings `model` with a trivial /api/chat call. Returns None if it
    responded normally (2xx), or a short human-readable problem description
    otherwise.

    Deliberately a real /api/chat call, not a check against /api/tags:
    /api/tags kept listing qwen3-coder:480b-cloud as present the entire time
    it sat retired on Ollama's cloud side (confirmed live 2026-08-02) — a
    retired model doesn't disappear from the local daemon's model list, it
    just 410s on actual use. Existence in /api/tags proves nothing about
    liveness.

    Sends keep_alive (same value ollama_ai.py's real chat calls use) so a
    healthy-but-cold model this check wakes up stays warm for whoever uses
    it next, instead of evicting again immediately after this ping."""
    try:
        async with httpx.AsyncClient(base_url=ollama_url, timeout=timeout) as http:
            resp = await http.post(
                "/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "stream": False,
                    "keep_alive": KEEP_ALIVE,
                },
            )
    except httpx.HTTPError as exc:
        return f"network error: {exc}"
    if resp.status_code < 400:
        return None
    try:
        detail = resp.json().get("error") or resp.text
    except ValueError:
        detail = resp.text
    return f"HTTP {resp.status_code}: {detail}".strip()


async def check_models(ollama_url: str, models: Iterable[CloudModel]) -> list[str]:
    """Runs check_model against every distinct model name in `models`,
    concurrently. Returns one formatted problem line per failing model
    (empty list = everything checked came back healthy). Multiple CloudModel
    entries sharing the same model string (e.g. two bots both defaulting to
    gpt-oss:20b-cloud) are pinged once and reported together.

    A model that fails the first ping is re-pinged once, after a short
    delay, before being reported — only a failure confirmed on BOTH attempts
    is treated as real. This only adds latency on the (rare) failing path;
    an all-healthy sweep (the common case) is exactly as fast as before."""
    by_name: dict[str, list[str]] = defaultdict(list)
    for model in models:
        by_name[model.name].append(model.used_by)

    names = list(by_name)
    results = await asyncio.gather(*(check_model(ollama_url, name) for name in names))
    failed = [name for name, result in zip(names, results, strict=True) if result is not None]

    if failed:
        await asyncio.sleep(_RETRY_DELAY_S)
        retry_results = await asyncio.gather(*(check_model(ollama_url, name) for name in failed))
        results = [
            retry_results[failed.index(name)] if name in failed else result
            for name, result in zip(names, results, strict=True)
        ]

    problems = []
    for name, result in zip(names, results, strict=True):
        if result is not None:
            used_by = ", ".join(by_name[name])
            problems.append(f"{name} ({used_by}): {result}")
    return problems
