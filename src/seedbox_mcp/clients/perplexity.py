from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import httpx

from seedbox_mcp.errors import UpstreamError

# Perplexity's web-grounded Chat Completions API. The `sonar` model returns a
# synthesized, cited answer in one call — far leaner than dumping raw page
# content into the bot's context (measured ~25-30x fewer tokens than Ollama
# web_search on release-status questions), and it answers the question
# directly instead of returning results to re-read. Used for release/
# availability-timing questions ("is X out / streaming yet"); general
# research still goes through Ollama web_search. Same key the LogueOS
# Gateway uses (PERPLEXITY_API_KEY).
PERPLEXITY_BASE_URL = "https://api.perplexity.ai"
PERPLEXITY_MODEL = "sonar"


class PerplexityClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def search(self, query: str, max_tokens: int = 500) -> dict[str, Any]:
        """Returns {"answer": str, "citations": list}. Capped max_tokens keeps
        the answer concise (release-status answers don't need long prose)."""
        tok = max(64, min(max_tokens, 4096))
        # Give Perplexity today's date and demand a front-loaded, concise
        # answer. Without this its answers ramble and can lead with a
        # self-contradictory line (observed: "Superman 2025" made it treat
        # the year as the current time and open with "not out yet" before
        # eventually concluding "yes, out" — the downstream model read the
        # wrong first sentence). "Treat any year in the title as the title"
        # stops that specific confusion.
        today = datetime.now().strftime("%B %-d, %Y")
        system = (
            f"Today's date is {today}. The user is asking whether a movie, show, or anime is "
            "available to watch (released / streaming / a new season out). "
            "Apply this rule strictly: compare each relevant date to today. If a theatrical, "
            "streaming, or digital release date is ON or BEFORE today, that thing HAS happened and "
            "IS available now. If it is AFTER today, it has NOT happened yet. A year in the title "
            "(e.g. 'Superman 2025') is part of the title, never the current time. "
            "Answer in 1-3 short sentences and LEAD with an unambiguous YES (it is available to "
            "watch right now) or NO (not yet), consistent with that rule, then give the key date(s) "
            "and platform. Do not lead with 'not yet' for something whose date has already passed, "
            "and do not contradict yourself."
        )
        payload = {
            "model": PERPLEXITY_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": query},
            ],
            "max_tokens": tok,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{PERPLEXITY_BASE_URL}/chat/completions", headers=self.headers, json=payload
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "Perplexity is unreachable.",
                {"reason": exc.__class__.__name__},
            ) from exc
        if response.is_error:
            detail: Any
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:400]
            raise UpstreamError(
                "validation" if response.status_code < 500 else "upstream_unreachable",
                "Perplexity rejected the request." if response.status_code < 500 else "Perplexity error.",
                {"status_code": response.status_code, "body": detail},
            )
        data = cast(dict[str, Any], response.json())
        choices = data.get("choices") or []
        answer = (choices[0].get("message") or {}).get("content", "") if choices else ""
        return {"answer": answer, "citations": data.get("citations") or []}
