from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CloudModel:
    """One Ollama Cloud model (`*-cloud` suffix) referenced somewhere in this
    repo. Single source of truth for the model string — before this existed,
    each bot/job hardcoded its own literal, so when Ollama silently retired
    qwen3-coder:480b-cloud (2026-07-15) it had to be found by grepping across
    files, and one of the two copies (the friend bot's, hit on every single
    message) sat broken for 6+ days before anyone noticed. `used_by` is
    surfaced in liveness-check alert text so a failure names the actual
    feature, not just an opaque model string.

    ALL_MODELS below is what model_health.check_models() and monitor.py's
    deterministic liveness check sweep — add a new CloudModel here and it's
    covered automatically, no separate registration step."""

    name: str
    used_by: str


# Interactive/quick reply — small + fast, latency-sensitive. Shared default
# for both Telegram bots' normal chat turns.
DEFAULT_BOT_MODEL = CloudModel("gpt-oss:20b-cloud", "NAS Ops bot: default interactive reply model")

# Poster/cover OCR reconciliation needs more reliable multi-step reasoning
# (reconstruct garbled OCR fragments, verify with a follow-up search,
# disambiguate same-titled entries) than a quick text reply does — live
# testing found DEFAULT_BOT_MODEL genuinely inconsistent on this one task.
# Deliberately the most reliable model available, not just "bigger than
# default" — see telegram_bot.py's _handle_photo_message.
PHOTO_IDENTIFY_MODEL = CloudModel("qwen3.5:397b-cloud", "NAS Ops bot: photo/poster identify model")

# Investigation/diagnosis is inherently multi-step (check status, pull logs,
# correlate across tools, then act) — escalated to from DEFAULT_BOT_MODEL
# when the operator's message signals diagnostic intent. See telegram_bot.py.
INVESTIGATE_MODEL = CloudModel("deepseek-v4-pro:cloud", "NAS Ops bot: investigate/diagnose model")

# Friend-facing bot's only chat model — every message from an allowed friend
# routes through this one.
DEFAULT_FRIEND_BOT_MODEL = CloudModel("gpt-oss:20b-cloud", "Friend bot: default chat model")

# Scheduled daily digest — a background batch job, so quality over latency.
DEFAULT_DIGEST_MODEL = CloudModel("deepseek-v4-pro:cloud", "Digest: scheduled report model")

# Scheduled monitor cycle (every 30 min) — same batch-job tradeoff as digest.
DEFAULT_MONITOR_MODEL = CloudModel("deepseek-v4-pro:cloud", "Monitor: scheduled check-cycle model")

ALL_MODELS: tuple[CloudModel, ...] = (
    DEFAULT_BOT_MODEL,
    PHOTO_IDENTIFY_MODEL,
    INVESTIGATE_MODEL,
    DEFAULT_FRIEND_BOT_MODEL,
    DEFAULT_DIGEST_MODEL,
    DEFAULT_MONITOR_MODEL,
)
