"""One agent step on the operator's subscription models, used when Ollama Cloud
answers 429 (its weekly usage limit).

Operator rule (2026-09-26): Ollama models while they have quota, otherwise the
models his subscriptions pay for. Each backend here only picks the next reply or
tool call, answered as one JSON object; run_agent_turn still executes every MCP
tool call itself, through the allowlist, the preview/confirm gate, the entity-id
check and the rate limit.

Claude and Codex run with no tools at all, and both are held to STEP_SCHEMA so
a model can't answer in loose prose. Cursor keeps its own read tools. Codex and
Cursor run in a bubblewrap jail whose home holds only their own login state:
the conversation and tool results are untrusted text and must not be able to
steer them into reading anything else on this machine. The real binaries are
called, never the agent-defaults wrappers on an interactive PATH, which add
permission-skipping flags.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("seedbox_mcp.chat.subscription_models")

HOME = Path.home()
BIN = HOME / ".local/bin"
WORKDIR = HOME / ".local/state/seedbox-mcp/model-fallback"
CLAUDE_MODEL = "sonnet"
CODEX_MODEL = "gpt-6-sol"
LUNA_MODEL = "gpt-6-luna"
CURSOR_MODEL = "grok-4.7-medium"

ALL_BACKENDS: tuple[str, ...] = ("claude", "codex", "cursor")
# Cursor can still read its own login state inside the jail, so a chat with
# people outside the household never reaches it.
FRIEND_BACKENDS: tuple[str, ...] = ("claude", "luna")

# Paths under HOME each jailed backend needs: its state (read-write) and its program (read-only).
_JAIL_STATE = {"codex": (".codex",), "cursor": (".cursor", ".config/cursor")}
_JAIL_PROGRAM = {"codex": (), "cursor": (".local/share/cursor-agent",)}

INSTRUCTIONS = """You are the model inside a tool-using assistant. Reply to the conversation \
below with ONE JSON object and nothing else, shaped:
{"content": "<text for the user; empty when you are calling tools>", \
"tool_calls": [{"name": "<tool name>", "arguments": "<the arguments object, JSON-encoded as a string>"}]}
Use "tool_calls": [] when you give your final answer. If the system prompt asks for the final \
answer in a format of its own (a JSON array, say), put that whole text as a string in "content". \
Call only tools listed under TOOLS, with arguments that match their schema. You cannot run \
anything yourself: the tools are run for you and their results come back as "tool" messages, \
in the order you called them, each naming its tool in "tool_name". A result already in the \
conversation is final: use it, never repeat the same call."""


# Tool arguments travel as a JSON string because OpenAI's strict schemas can't
# describe an object with free-form keys.
STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "tool_calls"],
    "properties": {
        "content": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "arguments"],
                "properties": {"name": {"type": "string"}, "arguments": {"type": "string"}},
            },
        },
    },
}
# Codex exec has no tool switch of its own; these features are every way it
# could read, run or fetch anything.
CODEX_TOOL_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apps",
    "browser_use",
    "computer_use",
    "skill_search",
    "tool_suggest",
)


class SubscriptionModelsUnavailable(RuntimeError):
    pass


def render_prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
    system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
    rest = _name_tool_results([_without_images(m) for m in (messages[1:] if system else messages)])
    return "\n\n".join(
        [
            INSTRUCTIONS,
            "SYSTEM PROMPT:\n" + system,
            "TOOLS:\n" + json.dumps(tools),
            "CONVERSATION (oldest first):\n" + json.dumps(rest),
        ]
    )


def _without_images(message: dict[str, Any]) -> dict[str, Any]:
    count = len(message.get("images") or [])
    if not count:
        return message
    stripped = {k: v for k, v in message.items() if k != "images"}
    stripped["content"] = f"{stripped.get('content', '')}\n[{count} image(s) attached to this message]"
    return stripped


def _name_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Labels each tool result with the tool it answers. Unlabeled, GPT models
    don't match results to their calls and repeat the same call until the
    round budget runs out."""
    named: list[dict[str, Any]] = []
    pending: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            pending = [(c.get("function") or {}).get("name", "") for c in message.get("tool_calls") or []]
        elif message.get("role") == "tool" and pending:
            message = {"role": "tool", "tool_name": pending.pop(0), **message}
        named.append(message)
    return named


def conversation_images(messages: list[dict[str, Any]]) -> list[str]:
    return [image for m in messages for image in m.get("images") or []]


def parse_step(text: str) -> dict[str, Any]:
    """The Ollama-shaped assistant message in a backend's answer; ValueError when there is none."""
    if text.lstrip().startswith("["):
        # A bare JSON array is the caller's own final-answer format (monitor, digest), sent unwrapped.
        json.loads(text)
        return {"content": text.strip(), "tool_calls": []}
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in answer: {text[:160]!r}")
    # The first complete object is the step; models sometimes add a second one or prose after it.
    answer, _ = json.JSONDecoder().raw_decode(text, start)
    content = answer.get("content") if isinstance(answer, dict) else None
    calls = answer.get("tool_calls") if isinstance(answer, dict) else None
    if not isinstance(content, str) or not isinstance(calls, list):
        raise ValueError(f"answer is not {{content, tool_calls}}: {text[:160]!r}")
    tool_calls = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            raise ValueError(f"malformed tool call: {call!r}")
        arguments = call.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"tool call arguments are not JSON: {call!r}") from exc
        if not isinstance(arguments, dict):
            raise ValueError(f"tool call arguments are not an object: {call!r}")
        tool_calls.append({"function": {"name": call["name"], "arguments": arguments}})
    return {"content": content, "tool_calls": tool_calls}


def _stream_result(out: str) -> str:
    for line in reversed(out.splitlines()):
        event = json.loads(line) if line.strip().startswith("{") else {}
        if event.get("type") == "result":
            if event.get("is_error"):
                raise ValueError(f"CLI reported an error: {line[:160]!r}")
            if isinstance(event.get("structured_output"), dict):
                return json.dumps(event["structured_output"])
            if not isinstance(event.get("result"), str):
                raise ValueError(f"no result text in CLI output: {line[:160]!r}")
            return event["result"]
    raise ValueError(f"no result event in CLI output: {out[-160:]!r}")


def _envelope_result(out: str) -> str:
    envelope = json.loads(out)
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(result, str):
        raise ValueError(f"no result text in CLI output: {out[:160]!r}")
    return result


def _jail(backend: str) -> list[str]:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise OSError(f"bubblewrap (bwrap) is not installed; {backend} is not run without its jail")
    prefix = [
        bwrap,
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/sbin", "/sbin",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", str(HOME),
        "--unshare-all", "--share-net", "--die-with-parent", "--new-session",
    ]  # fmt: skip
    resolver = Path("/run/systemd/resolve")
    if resolver.is_dir():
        prefix += ["--ro-bind", str(resolver), str(resolver)]
    for rel in _JAIL_STATE[backend]:
        if (HOME / rel).is_dir():
            prefix += ["--bind", str(HOME / rel), str(HOME / rel)]
    for rel in _JAIL_PROGRAM[backend]:
        if (HOME / rel).is_dir():
            prefix += ["--ro-bind", str(HOME / rel), str(HOME / rel)]
    return [*prefix, "--bind", str(WORKDIR), str(WORKDIR), "--chdir", str(WORKDIR)]


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
    finally:
        # A timeout or a cancelled turn must not leave a CLI running on the subscription.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"exited {proc.returncode}: {err.decode(errors='replace')[:200]}")
    return out.decode(errors="replace")


async def _claude(prompt: str, timeout_s: float, images: list[str]) -> str:
    # Telegram re-encodes every photo it delivers as JPEG.
    content: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}} for image in images
    ]
    content.append({"type": "text", "text": prompt})
    user_turn = {"type": "user", "message": {"role": "user", "content": content}}
    # --setting-sources "" keeps the operator's hooks and MCP servers out; no
    # session is saved, so these calls never show up as his own conversations.
    out = await _run(
        [
            str((BIN / "claude").resolve()), "-p", "--model", CLAUDE_MODEL, "--tools", "",
            "--setting-sources", "", "--no-session-persistence", "--disable-slash-commands",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(STEP_SCHEMA),
            "--system-prompt", "Answer with exactly the one JSON object the user message asks for.",
        ],
        json.dumps(user_turn) + "\n",
        timeout_s,
    )  # fmt: skip
    return _stream_result(out)


def _codex_secrets() -> list[str]:
    try:
        auth = json.loads((HOME / ".codex/auth.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    values = [auth.get("OPENAI_API_KEY"), *(auth.get("tokens") or {}).values()]
    return [v for v in values if isinstance(v, str) and len(v) >= SECRET_WINDOW]


# Any run of this many characters from a login token counts as a leak.
SECRET_WINDOW = 24


def leaks_codex_login(answer: str) -> bool:
    """Codex's login state sits inside its jail. It runs with every tool
    feature off, so it can't read it; this is the check that doesn't depend
    on that list staying complete across Codex releases."""
    return any(
        secret[i : i + SECRET_WINDOW] in answer
        for secret in _codex_secrets()
        for i in range(len(secret) - SECRET_WINDOW + 1)
    )


async def _codex_exec(model: str, prompt: str, timeout_s: float, images: list[str]) -> str:
    # A private (0700) directory per call: concurrent chats never share images or answers.
    call_dir = Path(tempfile.mkdtemp(prefix="codex-", dir=WORKDIR))
    try:
        answer = call_dir / "answer.txt"
        schema = call_dir / "schema.json"
        schema.write_text(json.dumps(STEP_SCHEMA))
        image_files = [call_dir / f"image-{i}.jpg" for i in range(len(images))]
        for path, image in zip(image_files, images, strict=True):
            path.write_bytes(base64.b64decode(image))
        disabled = [arg for feature in CODEX_TOOL_FEATURES for arg in ("--disable", feature)]
        attached = [arg for path in image_files for arg in ("-i", str(path))]
        # --ignore-user-config keeps the operator's MCP servers, hooks and profiles out.
        await _run(
            [
                *_jail("codex"), str((BIN / "codex").resolve()), "exec", "--skip-git-repo-check",
                "--sandbox", "read-only", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                *disabled, "-c", 'web_search="disabled"', "-m", model, *attached,
                "--output-schema", str(schema), "-C", str(call_dir), "-o", str(answer), "-",
            ],
            prompt,
            timeout_s,
        )  # fmt: skip
        text = answer.read_text()
    finally:
        shutil.rmtree(call_dir, ignore_errors=True)
    if leaks_codex_login(text):
        raise ValueError("answer contains Codex login material; discarded")
    return text


async def _codex(prompt: str, timeout_s: float, images: list[str]) -> str:
    return await _codex_exec(CODEX_MODEL, prompt, timeout_s, images)


async def _luna(prompt: str, timeout_s: float, images: list[str]) -> str:
    return await _codex_exec(LUNA_MODEL, prompt, timeout_s, images)


# Cursor gets no image bytes, only the "[image(s) attached]" note in the prompt.
async def _cursor(prompt: str, timeout_s: float, images: list[str]) -> str:
    cursor = str((BIN / "cursor-agent").resolve())
    # Cursor's login token lasts an hour; `status` renews it before the real call.
    await _run([*_jail("cursor"), cursor, "status"], "", 60.0)
    out = await _run(
        [
            *_jail("cursor"),
            cursor,
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
    return _envelope_result(out)


RUNNERS: dict[str, Callable[[str, float, list[str]], Awaitable[str]]] = {
    "claude": _claude,
    "codex": _codex,
    "luna": _luna,
    "cursor": _cursor,
}


async def step(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    backends: tuple[str, ...],
    timeout_s: float,
) -> tuple[dict[str, Any], str]:
    """(assistant message, backend that answered) from the first backend that gives a usable answer."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    prompt = render_prompt(messages, tools)
    images = conversation_images(messages)
    failures = []
    for name in backends:
        try:
            return parse_step(await RUNNERS[name](prompt, timeout_s, images)), name
        except Exception as exc:  # noqa: BLE001 - any CLI failure or odd output moves on to the next backend
            logger.warning("subscription model %s failed: %s: %s", name, type(exc).__name__, exc)
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    raise SubscriptionModelsUnavailable("; ".join(failures) or "no backends configured")
