from __future__ import annotations

import argparse
import asyncio
import logging

from fastmcp import Client
from pydantic import Field

from seedbox_mcp.chat.ollama_ai import DEFAULT_OLLAMA_URL, run_agent_turn
from seedbox_mcp.config import Settings

logger = logging.getLogger("seedbox_mcp.digest")

# Cloud-tagged, flat-rate under the operator's Ollama Pro subscription — see
# reference: smaller/faster models for latency-sensitive replies, bigger ones
# for batch judgment calls where quality matters more than turnaround.
DEFAULT_DIGEST_MODEL = "qwen3-coder:480b-cloud"

SYSTEM_PROMPT = """\
You are a NAS housekeeping assistant. You run on a schedule, not in a chat — \
nobody is watching live, so write one self-contained report, not a conversation.

You have read-only tools for: Plex/Radarr/Sonarr staleness (staleness_report, \
media_status), and NAS storage outside the Plex library (nas_backup_health, \
nas_storage_inventory — music samples, production kits, the general \
transfer/drop folder).

Call the tools relevant to the task you're given, then write a short plain-\
English digest:
- Lead with anything that needs the operator's attention (a failed/stale \
backup, a stuck download, runaway storage growth). If nothing does, say so \
plainly — do not invent urgency.
- Group the rest by area (backups / media / other storage).
- Be concrete: name the thing, the number, the age. "3 movies added 6+ \
months ago, never watched, 41GB" beats "some old content was found."
- You have no write or delete tools right now — never imply you took an \
action, only that you found something. Recommending a follow-up is fine.
- Keep it under ~300 words unless something genuinely needs more detail.
"""


class DigestSettings(Settings):
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_digest_model: str = DEFAULT_DIGEST_MODEL

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"


async def run_digest(task: str, model: str | None = None) -> str:
    settings = DigestSettings()  # type: ignore[call-arg]
    mcp_client = Client(settings.mcp_url, auth=settings.mcp_bearer_token.get_secret_value())
    return await run_agent_turn(
        task,
        system_prompt=SYSTEM_PROMPT,
        mcp_client=mcp_client,
        model=model or settings.ollama_digest_model,
        ollama_url=settings.ollama_url,
    )


DEFAULT_TASK = (
    "Run the routine NAS health check: staleness_report (movies+tv, "
    "older_than_days=120, include_missing=true), media_status, "
    "nas_backup_health, and nas_storage_inventory. Summarize."
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Run a one-shot NAS housekeeping digest.")
    parser.add_argument("--task", default=DEFAULT_TASK, help="Override the digest task prompt.")
    parser.add_argument("--model", default=None, help=f"Ollama model tag (default: {DEFAULT_DIGEST_MODEL}).")
    args = parser.parse_args()

    result = asyncio.run(run_digest(args.task, args.model))
    print(result)


if __name__ == "__main__":
    main()
