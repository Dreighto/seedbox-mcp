"""One agent step on the operator's subscription models, used when Ollama Cloud
answers 429 (its weekly usage limit).

Operator rule (2026-09-26): Ollama models while they have quota, otherwise the
models his subscriptions pay for. Each backend here only picks the next reply or
tool call, answered as one JSON object; run_agent_turn still executes every tool
call itself, so the allowlist, the preview/confirm gate, the entity-id check and
the rate limit apply exactly as they do for Ollama.

The real binaries in ~/.local/bin are called, never the agent-defaults wrappers
earlier on an interactive PATH: those add permission-skipping flags.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("seedbox_mcp.chat.subscription_models")

HOME = Path.home()
BIN = HOME / ".local/bin"
WORKDIR = HOME / ".local/state/seedbox-mcp/model-fallback"
CLAUDE_MODEL = "sonnet"
CURSOR_MODEL = "grok-4.7-medium"

ALL_BACKENDS: tuple[str, ...] = ("claude", "codex", "cursor")
# Codex and Cursor can still read files on this machine (read-only), so a chat
# with people outside the household gets Claude only, which runs with no tools.
NO_FILE_ACCESS: tuple[str, ...] = ("claude",)

INSTRUCTIONS = """You are the model inside a tool-using assistant. Reply to the conversation \
below with ONE JSON object and nothing else, shaped:
{"content": "<text for the user; empty when you are calling tools>", \
"tool_calls": [{"name": "<tool name>", "arguments": {<arguments>}}]}
Use "tool_calls": [] when you give your final answer. Call only tools listed under TOOLS, \
with arguments that match their schema. You cannot run anything yourself: the tools are run \
for you and their results come back as "tool" messages, in the order you called them."""


class SubscriptionModelsUnavailable(RuntimeError):
    pass


def render_prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
    system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
    rest = messages[1:] if system else messages
    return "\n\n".join(
        [
            INSTRUCTIONS,
            "SYSTEM PROMPT:\n" + system,
            "TOOLS:\n" + json.dumps(tools),
            "CONVERSATION (oldest first):\n" + json.dumps(rest),
        ]
    )


def parse_step(text: str) -> dict[str, Any]:
    """The Ollama-shaped assistant message in a backend's answer; ValueError when there is none."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in answer: {text[:160]!r}")
    answer = json.loads(text[start : end + 1])
    content = answer.get("content") if isinstance(answer, dict) else None
    calls = answer.get("tool_calls") if isinstance(answer, dict) else None
    if not isinstance(content, str) or not isinstance(calls, list):
        raise ValueError(f"answer is not {{content, tool_calls}}: {text[:160]!r}")
    tool_calls = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            raise ValueError(f"malformed tool call: {call!r}")
        arguments = call.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError(f"tool call arguments are not an object: {call!r}")
        tool_calls.append({"function": {"name": call["name"], "arguments": arguments}})
    return {"content": content, "tool_calls": tool_calls}


def _child_env() -> dict[str, str]:
    return {
        "PATH": f"{BIN}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
    }


async def _run(argv: list[str], stdin: str, timeout_s: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=WORKDIR,
        env=_child_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(argv[0]).name} exited {proc.returncode}: {err.decode(errors='replace')[:200]}")
    return out.decode(errors="replace")


async def _claude(prompt: str, timeout_s: float) -> str:
    # --setting-sources "" keeps the operator's hooks and MCP servers out; no
    # session is saved, so these calls never show up as his own conversations.
    out = await _run(
        [
            str(BIN / "claude"),
            "-p",
            "--model",
            CLAUDE_MODEL,
            "--tools",
            "",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--system-prompt",
            "Answer with exactly the one JSON object the user message asks for.",
        ],
        prompt,
        timeout_s,
    )
    return str(json.loads(out)["result"])


async def _codex(prompt: str, timeout_s: float) -> str:
    answer = WORKDIR / f"codex-{os.getpid()}-{time.time_ns()}.txt"
    try:
        await _run(
            [
                str(BIN / "codex"),
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "-C",
                str(WORKDIR),
                "-o",
                str(answer),
                "-",
            ],
            prompt,
            timeout_s,
        )
        return answer.read_text()
    finally:
        answer.unlink(missing_ok=True)


async def _cursor(prompt: str, timeout_s: float) -> str:
    # Cursor's login token lasts an hour; `status` renews it before the real call.
    await _run([str(BIN / "cursor-agent"), "status"], "", 60.0)
    out = await _run(
        [
            str(BIN / "cursor-agent"),
            "-p",
            "--mode",
            "ask",
            "--output-format",
            "json",
            "--model",
            CURSOR_MODEL,
            "--trust",
        ],
        prompt,
        timeout_s,
    )
    return str(json.loads(out)["result"])


RUNNERS: dict[str, Callable[[str, float], Awaitable[str]]] = {"claude": _claude, "codex": _codex, "cursor": _cursor}


async def step(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    backends: tuple[str, ...],
    timeout_s: float,
) -> tuple[dict[str, Any], str]:
    """(assistant message, backend that answered) from the first backend that gives a usable answer."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    prompt = render_prompt(messages, tools)
    failures = []
    for name in backends:
        try:
            return parse_step(await RUNNERS[name](prompt, timeout_s)), name
        except (OSError, RuntimeError, ValueError, KeyError, TimeoutError) as exc:
            logger.warning("subscription model %s failed: %s", name, exc)
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    raise SubscriptionModelsUnavailable("; ".join(failures) or "no backends configured")
