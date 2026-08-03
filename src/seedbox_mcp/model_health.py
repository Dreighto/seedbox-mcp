from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable

import httpx

from seedbox_mcp.model_registry import CloudModel

# Trivial single-turn ping, no tools, no keep_alive override — this exists
# purely to provoke a real response (or a real error) from the model, not to
# warm it for a subsequent call (see monitor.py's _keep_interactive_model_warm
# for that, separate concern).
_PING_TIMEOUT_S = 30.0


async def check_model(ollama_url: str, model: str, timeout: float = _PING_TIMEOUT_S) -> str | None:
    """Pings `model` with a trivial /api/chat call. Returns None if it
    responded normally (2xx), or a short human-readable problem description
    otherwise.

    Deliberately a real /api/chat call, not a check against /api/tags:
    /api/tags kept listing qwen3-coder:480b-cloud as present the entire time
    it sat retired on Ollama's cloud side (confirmed live 2026-08-02) — a
    retired model doesn't disappear from the local daemon's model list, it
    just 410s on actual use. Existence in /api/tags proves nothing about
    liveness."""
    try:
        async with httpx.AsyncClient(base_url=ollama_url, timeout=timeout) as http:
            resp = await http.post(
                "/api/chat",
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "stream": False},
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
    gpt-oss:20b-cloud) are pinged once and reported together."""
    by_name: dict[str, list[str]] = defaultdict(list)
    for model in models:
        by_name[model.name].append(model.used_by)

    names = list(by_name)
    results = await asyncio.gather(*(check_model(ollama_url, name) for name in names))

    problems = []
    for name, result in zip(names, results, strict=True):
        if result is not None:
            used_by = ", ".join(by_name[name])
            problems.append(f"{name} ({used_by}): {result}")
    return problems
