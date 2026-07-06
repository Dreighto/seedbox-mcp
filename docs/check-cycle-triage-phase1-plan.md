# Check-Cycle Triage Surface, Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the whole-NAS check cycle into a scannable triage report where the model returns structured findings and code owns formatting, on both the scheduled monitor and a new on-demand command.

**Architecture:** A new `triage.py` module defines the `Finding` shape, the JSON the model must emit, a tolerant parser with a safe fallback, an HTML renderer, and an actionable-only fingerprint for alert dedup. `monitor.py`, `telegram_bot.py`, and `digest.py` produce findings by appending a fixed instruction to their model task, parse the result, and render through the shared renderer. No buttons and no callbacks in Phase 1.

**Tech Stack:** Python 3, asyncio, httpx, pytest. Ollama-served models via the existing `run_agent_turn`. No new third-party dependencies (validation is hand-rolled, no `jsonschema`).

## Global Constraints

- Linux-first. No Windows paths.
- No new third-party dependency. Validate findings by hand, do not add `jsonschema`.
- No-slop writing in all user-facing strings: no em-dashes or en-dashes, no filler words, no corporate words. The dash strip in `telegram.py` is the deterministic backstop; still write clean strings.
- Do NOT implement Phase 2 (no inline buttons, no `callback_query`, no agent dispatch).
- Do NOT change the graduation gate or which actions are graduated.
- The 12 append-only JSONL files in the kernel are irrelevant here; this repo's state files (`.monitor_alert_state.json`) are read-modify-write JSON and stay that way.
- Every model-facing prompt string stays ASCII-clean; every user-facing string is escaped by the renderer before it reaches Telegram HTML.
- Run tests with `.venv/bin/python -m pytest`.

---

## File Structure

- Create `src/seedbox_mcp/triage.py`: the shared engine. `Finding`, `TRIAGE_SCHEMA`, `FINDINGS_INSTRUCTION`, `parse_findings`, `render_triage`, `fingerprint`, `slugify`.
- Modify `src/seedbox_mcp/telegram.py`: add `strip_dashes` (factored out) and `send_message_html`.
- Modify `src/seedbox_mcp/monitor.py`: `run_monitor_cycle` returns `list[Finding]`; deterministic notes become auto-fixed findings; `main` renders and pushes only when actionable.
- Modify `src/seedbox_mcp/telegram_bot.py`: on-demand "full status" intent runs the cycle and renders.
- Modify `src/seedbox_mcp/digest.py`: emit findings and render through `render_triage`.
- Modify `evals/bot_eval.py`: add a triage-cycle structural assertion.
- Create `tests/test_triage.py`: unit tests for the pure functions.

---

## Task 1: Triage data model, schema, and parser

**Files:**

- Create: `src/seedbox_mcp/triage.py`
- Test: `tests/test_triage.py`

**Interfaces:**

- Produces:
  - `@dataclass Finding` with fields `id: str`, `severity: str`, `title: str`, `real: bool`, `reason: str`, `recommendation: str = ""`, `fixable_by: str = "none"`, `evidence: str = ""`, `auto_fixed: bool = False`.
  - `SEVERITIES = ("needs_fix", "watch", "healthy")`, `FIXABLE_BY = ("proven", "tap", "agent", "none")`.
  - `FINDINGS_INSTRUCTION: str` (prompt fragment telling the model to emit the JSON array).
  - `slugify(text: str) -> str`.
  - `parse_findings(text: str) -> list[Finding]` (tolerant extract, validate, fallback).

- [ ] **Step 1: Write the failing test**

````python
# tests/test_triage.py
from seedbox_mcp.triage import Finding, parse_findings, slugify


def test_slugify_basic():
    assert slugify("Sonarr import stuck: The Irishman") == "sonarr-import-stuck-the-irishman"
    assert slugify("  Disk 92% !!") == "disk-92"


def test_parse_findings_plain_array():
    text = '[{"severity":"needs_fix","title":"Import stuck","real":true,"reason":"queue empty","recommendation":"fix_import","fixable_by":"tap","evidence":"tmdb 398978"}]'
    findings = parse_findings(text)
    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, Finding)
    assert f.severity == "needs_fix"
    assert f.title == "Import stuck"
    assert f.real is True
    assert f.fixable_by == "tap"
    assert f.id == "import-stuck"
    assert f.auto_fixed is False


def test_parse_findings_strips_code_fence():
    text = 'here is the report:\n```json\n[{"severity":"healthy","title":"Disks OK","real":true,"reason":"all pass"}]\n```\n'
    findings = parse_findings(text)
    assert len(findings) == 1
    assert findings[0].severity == "healthy"
    assert findings[0].recommendation == ""
    assert findings[0].fixable_by == "none"


def test_parse_findings_dedupes_ids():
    text = '[{"severity":"watch","title":"Same","real":true,"reason":"a"},{"severity":"watch","title":"Same","real":true,"reason":"b"}]'
    findings = parse_findings(text)
    assert [f.id for f in findings] == ["same", "same-2"]


def test_parse_findings_bad_json_falls_back_without_losing_text():
    text = "the queue looks stalled and I could not format this"
    findings = parse_findings(text)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "needs_fix"
    assert f.real is True
    assert "could not" in f.reason.lower() or "unstructured" in f.reason.lower()
    assert "stalled" in f.evidence


def test_parse_findings_drops_invalid_items_keeps_valid():
    text = '[{"severity":"bogus","title":"x","real":true,"reason":"y"},{"severity":"watch","title":"Good","real":false,"reason":"z"}]'
    findings = parse_findings(text)
    assert [f.title for f in findings] == ["Good"]
````

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_triage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'seedbox_mcp.triage'`.

- [ ] **Step 3: Write the minimal implementation**

````python
# src/seedbox_mcp/triage.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass

SEVERITIES = ("needs_fix", "watch", "healthy")
FIXABLE_BY = ("proven", "tap", "agent", "none")


@dataclass
class Finding:
    id: str
    severity: str
    title: str
    real: bool
    reason: str
    recommendation: str = ""
    fixable_by: str = "none"
    evidence: str = ""
    auto_fixed: bool = False


FINDINGS_INSTRUCTION = (
    "After you finish the checks, output ONLY a JSON array of findings and nothing "
    "else, no prose before or after. One object per thing you checked. Each object: "
    '"severity" (one of "needs_fix", "watch", "healthy"), "title" (one short line), '
    '"real" (true if it is a genuine problem, false if it is a false alarm), "reason" '
    '(why, one sentence), "recommendation" (what should happen, empty for healthy), '
    '"fixable_by" (one of "proven", "tap", "agent", "none"), "evidence" (IDs, paths, '
    "or quoted numbers, may be empty). Report every check as its own finding, healthy "
    "ones included. Do not wrap the array in markdown."
)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "finding"


def _extract_array(text: str) -> list | None:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        loaded = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, list) else None


def _valid_item(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("severity") not in SEVERITIES:
        return False
    if not isinstance(item.get("title"), str) or not item["title"].strip():
        return False
    if not isinstance(item.get("real"), bool):
        return False
    if not isinstance(item.get("reason"), str):
        return False
    fb = item.get("fixable_by", "none")
    return fb in FIXABLE_BY


def _fallback(text: str) -> list[Finding]:
    return [
        Finding(
            id="unstructured-cycle-output",
            severity="needs_fix",
            title="Check cycle output could not be structured",
            real=True,
            reason="the model returned output that was not a valid findings array",
            recommendation="read the raw output below and re-run the cycle",
            fixable_by="none",
            evidence=text.strip()[:500],
        )
    ]


def parse_findings(text: str) -> list[Finding]:
    array = _extract_array(text)
    if array is None:
        return _fallback(text)
    findings: list[Finding] = []
    seen: dict[str, int] = {}
    for item in array:
        if not _valid_item(item):
            continue
        base = slugify(item["title"])
        seen[base] = seen.get(base, 0) + 1
        fid = base if seen[base] == 1 else f"{base}-{seen[base]}"
        findings.append(
            Finding(
                id=fid,
                severity=item["severity"],
                title=item["title"].strip(),
                real=item["real"],
                reason=item.get("reason", "").strip(),
                recommendation=item.get("recommendation", "").strip(),
                fixable_by=item.get("fixable_by", "none"),
                evidence=str(item.get("evidence", "")).strip(),
            )
        )
    return findings or _fallback(text)
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_triage.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/seedbox_mcp/triage.py tests/test_triage.py
git commit -m "feat(triage): Finding model, schema instruction, tolerant parser"
```

---

## Task 2: Triage renderer and fingerprint

**Files:**

- Modify: `src/seedbox_mcp/triage.py`
- Test: `tests/test_triage.py`

**Interfaces:**

- Consumes: `Finding`, `SEVERITIES` from Task 1.
- Produces:
  - `render_triage(findings: list[Finding], *, interactive: bool = False) -> tuple[str, dict | None]`. In Phase 1 always returns `(html_text, None)`.
  - `fingerprint(findings: list[Finding]) -> str | None`. Hash over actionable findings only (`needs_fix`, `watch`); `None` when none are actionable.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_triage.py
from seedbox_mcp.triage import render_triage, fingerprint


def _f(**kw):
    base = dict(id="x", severity="healthy", title="t", real=True, reason="r")
    base.update(kw)
    return Finding(**base)


def test_render_groups_and_counts():
    findings = [
        _f(id="a", severity="needs_fix", title="Import stuck", recommendation="fix_import", evidence="tmdb 1"),
        _f(id="b", severity="watch", title="2 requests waiting"),
        _f(id="c", severity="healthy", title="Disks"),
        _f(id="d", severity="healthy", title="Fleet"),
        _f(id="e", severity="watch", title="Queue resumed", auto_fixed=True),
    ]
    text, markup = render_triage(findings)
    assert markup is None
    assert "2 need attention" in text
    assert "NEEDS FIX (1)" in text
    assert "WATCH (1)" in text
    assert "AUTO-FIXED THIS CYCLE (1)" in text
    assert "HEALTHY (2)" in text
    assert "Import stuck" in text
    assert "tmdb 1" in text


def test_render_escapes_html():
    text, _ = render_triage([_f(severity="needs_fix", title="A <b> & <i>", real=True, reason="x")])
    assert "<b>" not in text.replace("<b>", "")  # raw tag from title must be escaped
    assert "&lt;b&gt;" in text or "&amp;" in text


def test_render_all_healthy_has_no_attention_header():
    text, _ = render_triage([_f(severity="healthy", title="Disks")])
    assert "need attention" not in text
    assert "HEALTHY (1)" in text


def test_fingerprint_actionable_only_and_order_independent():
    a = [_f(id="1", severity="needs_fix", title="B"), _f(id="2", severity="watch", title="A")]
    b = [_f(id="9", severity="watch", title="A"), _f(id="8", severity="needs_fix", title="B")]
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_none_when_only_healthy_or_autofixed():
    assert fingerprint([_f(severity="healthy", title="Disks")]) is None
    assert fingerprint([_f(severity="watch", title="Q", auto_fixed=True)]) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_triage.py -k "render or fingerprint" -v`
Expected: FAIL with `ImportError: cannot import name 'render_triage'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# append to src/seedbox_mcp/triage.py
import hashlib
from html import escape

_ACTIONABLE = ("needs_fix", "watch")
_GROUP_LABELS = {"needs_fix": "NEEDS FIX", "watch": "WATCH"}


def fingerprint(findings: list[Finding]) -> str | None:
    keys = sorted(
        f"{f.severity}:{f.title}"
        for f in findings
        if f.severity in _ACTIONABLE and not f.auto_fixed
    )
    if not keys:
        return None
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def _render_finding(f: Finding) -> str:
    lines = [f"- {escape(f.title)}"]
    detail = f.reason
    if f.recommendation:
        detail = f"{detail} recommend: {f.recommendation}" if detail else f"recommend: {f.recommendation}"
    if detail:
        lines.append(f"  {escape(detail)}")
    if f.evidence:
        lines.append(f"  <code>{escape(f.evidence)}</code>")
    return "\n".join(lines)


def render_triage(findings: list[Finding], *, interactive: bool = False) -> tuple[str, dict | None]:
    active = [f for f in findings if not f.auto_fixed]
    auto = [f for f in findings if f.auto_fixed]
    attention = [f for f in active if f.severity in _ACTIONABLE]

    header = f"NAS OPS, {len(attention)} need attention" if attention else "NAS OPS, all healthy"
    blocks = [f"<b>{escape(header)}</b>"]

    for sev in _ACTIONABLE:
        group = [f for f in active if f.severity == sev]
        if group:
            blocks.append(f"<b>{_GROUP_LABELS[sev]} ({len(group)})</b>\n" + "\n".join(_render_finding(f) for f in group))

    if auto:
        body = "\n".join(f"- {escape(f.title)}: {escape(f.reason)}" for f in auto)
        blocks.append(f"<b>AUTO-FIXED THIS CYCLE ({len(auto)})</b>\n" + body)

    healthy = [f for f in active if f.severity == "healthy"]
    if healthy:
        names = ", ".join(escape(f.title) for f in healthy)
        blocks.append(f"<b>HEALTHY ({len(healthy)})</b>: {names}")

    return "\n\n".join(blocks), None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_triage.py -v`
Expected: PASS (all Task 1 and Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/seedbox_mcp/triage.py tests/test_triage.py
git commit -m "feat(triage): HTML renderer and actionable-only fingerprint"
```

---

## Task 3: HTML send path in telegram.py

**Files:**

- Modify: `src/seedbox_mcp/telegram.py`
- Test: `tests/test_telegram_html.py` (create)

**Interfaces:**

- Consumes: existing `MAX_MESSAGE_CHARS`, `TELEGRAM_API`, `format_for_telegram`.
- Produces:
  - `strip_dashes(text: str) -> str` (the dash-strip logic factored out of `format_for_telegram`).
  - `async send_message_html(token: str, chat_id: int, text: str, reply_markup: dict | None = None) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_html.py
from seedbox_mcp.telegram import strip_dashes


def test_strip_dashes_em_to_comma():
    assert strip_dashes("All set — Star Wars") == "All set, Star Wars"


def test_strip_dashes_numeric_range_keeps_hyphen():
    assert strip_dashes("1995-1999") == "1995-1999"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_telegram_html.py -v`
Expected: FAIL with `ImportError: cannot import name 'strip_dashes'`.

- [ ] **Step 3: Write the minimal implementation**

In `src/seedbox_mcp/telegram.py`, extract the two dash `re.sub` lines from `format_for_telegram` into a helper and call it from both places. Add the helper after the imports:

```python
def strip_dashes(text: str) -> str:
    """No em/en-dashes in any bot reply (operator's standing no-slop rule).
    A numeric range keeps a hyphen; any other dash becomes a comma."""
    text = re.sub(r"(?<=\d)\s*–\s*(?=\d)", "-", text)
    return re.sub(r"\s*[—–]\s*", ", ", text)
```

Replace the two dash `re.sub` lines inside `format_for_telegram` (currently the lines assigning `text = re.sub(...)` for the dashes) with:

```python
    text = strip_dashes(text)
```

Add the HTML sender at the end of the file:

```python
async def send_message_html(token: str, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    """Send an HTML-parse-mode message. The caller is responsible for escaping
    any model-supplied substrings (triage.render_triage does this). Applies the
    no-slop dash strip and the length guard, and falls back to plain text on a
    Telegram 400 so a formatting slip never loses the message."""
    body = strip_dashes(text)
    if len(body) > MAX_MESSAGE_CHARS:
        body = body[: MAX_MESSAGE_CHARS - 20] + "\n\n[truncated]"
    payload: dict = {"chat_id": chat_id, "text": body, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json=payload)
        if resp.status_code == 400:
            logger.warning("telegram HTML sendMessage rejected (%s), retrying as plain text", resp.text[:200])
            plain = {"chat_id": chat_id, "text": re.sub(r"<[^>]+>", "", body)}
            if reply_markup is not None:
                plain["reply_markup"] = reply_markup
            resp = await client.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json=plain)
    if resp.is_error:
        logger.error("telegram send_message_html failed: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_telegram_html.py tests/test_triage.py -v`
Expected: PASS. Also run the existing telegram tests if present: `.venv/bin/python -m pytest tests/ -k telegram -v` and confirm no regression.

- [ ] **Step 5: Commit**

```bash
git add src/seedbox_mcp/telegram.py tests/test_telegram_html.py
git commit -m "feat(telegram): send_message_html with plain-text fallback; factor strip_dashes"
```

---

## Task 4: Monitor produces findings and renders

**Files:**

- Modify: `src/seedbox_mcp/monitor.py`
- Test: `tests/test_monitor_triage.py` (create)

**Interfaces:**

- Consumes: `Finding`, `FINDINGS_INSTRUCTION`, `parse_findings`, `render_triage`, `fingerprint` from `triage`; `send_message_html` from `telegram`.
- Produces:
  - `run_monitor_cycle(model=None) -> list[Finding]` (was `-> str | None`).
  - `_notes_to_findings(queue_fix_note, strike_note, recovery_note) -> list[Finding]` (pure): each non-empty note becomes an `auto_fixed=True`, `severity="watch"` finding.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_monitor_triage.py
from seedbox_mcp.monitor import _notes_to_findings


def test_notes_become_autofixed_findings():
    out = _notes_to_findings("Queue was paused, resumed it.", None, "Restarted tautulli, verified up.")
    assert len(out) == 2
    assert all(f.auto_fixed for f in out)
    assert all(f.severity == "watch" for f in out)
    titles = " ".join(f.title for f in out).lower()
    assert "queue" in titles or "resumed" in titles


def test_notes_empty_gives_no_findings():
    assert _notes_to_findings(None, None, None) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_monitor_triage.py -v`
Expected: FAIL with `ImportError: cannot import name '_notes_to_findings'`.

- [ ] **Step 3: Write the minimal implementation**

In `src/seedbox_mcp/monitor.py`, add imports near the top:

```python
from seedbox_mcp.triage import FINDINGS_INSTRUCTION, Finding, fingerprint, parse_findings, render_triage, slugify
from seedbox_mcp.telegram import send_message_html
```

Add the pure helper above `run_monitor_cycle`:

```python
def _notes_to_findings(*notes: str | None) -> list[Finding]:
    """Each deterministic-fix note (queue resume, strike fix, service restart)
    becomes an auto-fixed finding so it renders in the AUTO-FIXED group."""
    out: list[Finding] = []
    for note in notes:
        if note and note.strip():
            title = note.strip().split(".")[0][:80]
            out.append(
                Finding(
                    id=slugify(title),
                    severity="watch",
                    title=title,
                    real=True,
                    reason=note.strip(),
                    fixable_by="proven",
                    auto_fixed=True,
                )
            )
    return out
```

Change the end of `run_monitor_cycle` (currently builds `parts` and returns joined text). Replace the model-call task string to append the findings instruction, and replace the return:

```python
    text, _history, _pending_action, _known_entity_ids = await run_agent_turn(
        task + "\n\n" + FINDINGS_INSTRUCTION,
        system_prompt=SYSTEM_PROMPT,
        mcp_client=mcp_client,
        model=model or settings.ollama_monitor_model,
        allowed_tools=MONITOR_READ_ONLY_TOOLS | MONITOR_ACTION_TOOLS | MONITOR_ESCALATION_TOOLS,
        action_tools=MONITOR_ACTION_TOOLS,
        escalation_tools=MONITOR_ESCALATION_TOOLS,
        ollama_url=settings.ollama_url,
        max_tool_rounds=20,
    )
    llm_findings = [] if text.strip() == NO_ALERT_SENTINEL else parse_findings(text)
    return _notes_to_findings(queue_fix_note, strike_note, recovery_note) + llm_findings
```

Update the `run_monitor_cycle` return type annotation to `-> list[Finding]` and its docstring to say it returns the findings (empty list = clean cycle).

Now update `main()` (currently checks `result is None`). Replace its body from `result = asyncio.run(...)` through the send with:

```python
    findings = asyncio.run(run_monitor_cycle(args.model))
    fp = fingerprint(findings)
    if fp is None and not args.force_alert_test:
        print(f"[{NO_ALERT_SENTINEL}] nothing actionable this cycle")
        _save_alert_state({})
        return
    should_push, new_state = _alert_decision(fp, _load_alert_state(), time.time())
    text, _markup = render_triage(findings)
    _save_alert_state(new_state)
    if not should_push and not args.force_alert_test:
        print("alert suppressed (duplicate within remind window)")
        return
    print(text)
    if not args.no_telegram:
        settings = MonitorSettings()  # type: ignore[call-arg]
        if settings.nas_ops_telegram_bot_token and settings.nas_ops_telegram_allowed_chat_id:
            asyncio.run(
                send_message_html(
                    settings.nas_ops_telegram_bot_token.get_secret_value(),
                    settings.nas_ops_telegram_allowed_chat_id,
                    text,
                )
            )
```

Keep persisting the rendered `text` into `.monitor_alert_state.json` if the current code does (the interactive bot reads it to resolve "investigate it"): set `new_state["alert_text"] = text` before `_save_alert_state(new_state)`, matching the existing key the bot reads.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_monitor_triage.py tests/test_triage.py -v`
Expected: PASS. Then a real dry run: `.venv/bin/python -m seedbox_mcp.monitor --no-telegram` and confirm it prints a grouped report or the no-alert line without a traceback.

- [ ] **Step 5: Commit**

```bash
git add src/seedbox_mcp/monitor.py tests/test_monitor_triage.py
git commit -m "feat(monitor): emit structured findings, render + fingerprint on push"
```

---

## Task 5: On-demand "full status" command in the ops bot

**Files:**

- Modify: `src/seedbox_mcp/telegram_bot.py`
- Test: `tests/test_status_intent.py` (create)

**Interfaces:**

- Consumes: `run_monitor_cycle` from `monitor`; `render_triage` from `triage`; `send_message_html` from `telegram`.
- Produces: `_is_status_request(text: str) -> bool` (pure).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_status_intent.py
from seedbox_mcp.telegram_bot import _is_status_request


def test_status_intent_matches():
    for s in ["full status", "run the checks", "status report", "run a check cycle", "how is everything"]:
        assert _is_status_request(s) is True


def test_status_intent_ignores_normal_queries():
    for s in ["add star wars", "is Dune on plex", "what's the queue"]:
        assert _is_status_request(s) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_status_intent.py -v`
Expected: FAIL with `ImportError: cannot import name '_is_status_request'`.

- [ ] **Step 3: Write the minimal implementation**

In `src/seedbox_mcp/telegram_bot.py` add near the other module-level regexes:

```python
import re as _re

_STATUS_INTENT_RE = _re.compile(
    r"\b(full status|status report|run (the )?checks?|run a? ?check cycle|check cycle|how is everything|status check)\b",
    _re.IGNORECASE,
)


def _is_status_request(text: str) -> bool:
    return bool(_STATUS_INTENT_RE.search(text or ""))
```

At the top of `_handle_message`, after the message text is available and before the normal `run_agent_turn` path, add the short-circuit:

```python
    if _is_status_request(text):
        from seedbox_mcp.monitor import run_monitor_cycle
        from seedbox_mcp.triage import render_triage
        from seedbox_mcp.telegram import send_message_html

        findings = await run_monitor_cycle()
        rendered, _markup = render_triage(findings)
        await send_message_html(token, chat_id, rendered)
        return
```

(Use the same `token`/`chat_id` variables the surrounding `_handle_message` already has; the import is local to avoid a circular import at module load, since `monitor` imports `telegram`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_status_intent.py -v`
Expected: PASS. Then confirm no import cycle: `.venv/bin/python -c "import seedbox_mcp.telegram_bot"` exits 0.

- [ ] **Step 5: Commit**

```bash
git add src/seedbox_mcp/telegram_bot.py tests/test_status_intent.py
git commit -m "feat(ops-bot): on-demand full-status intent renders the triage report"
```

---

## Task 6: Digest renders through the triage engine

**Files:**

- Modify: `src/seedbox_mcp/digest.py`
- Test: covered by the manual dry run below (digest is a thin orchestration change reusing tested helpers).

**Interfaces:**

- Consumes: `FINDINGS_INSTRUCTION`, `parse_findings`, `render_triage` from `triage`; `send_message_html` from `telegram`; existing `graduation_nudge`.

- [ ] **Step 1: Modify run_digest to request findings**

In `src/seedbox_mcp/digest.py`, add imports:

```python
from seedbox_mcp.triage import FINDINGS_INSTRUCTION, parse_findings, render_triage
from seedbox_mcp.telegram import send_message_html
```

In `run_digest`, append the instruction to the task passed to `run_agent_turn`:

```python
    text, _history, _pending_action, _known_entity_ids = await run_agent_turn(
        task + "\n\n" + FINDINGS_INSTRUCTION,
        ...  # keep all existing keyword args unchanged
    )
    return text
```

- [ ] **Step 2: Modify digest main to render**

In `digest.main`, replace the block that builds the report and sends it. After `report = asyncio.run(run_digest(...))` and `nudge = graduation_nudge()`:

```python
    findings = parse_findings(report)
    rendered, _markup = render_triage(findings)
    if nudge:
        rendered = rendered + "\n\n" + nudge
    print(rendered)
    if not args.no_telegram:
        settings = ...  # the same settings object main already builds
        if settings.nas_ops_telegram_bot_token and settings.nas_ops_telegram_allowed_chat_id:
            asyncio.run(
                send_message_html(
                    settings.nas_ops_telegram_bot_token.get_secret_value(),
                    settings.nas_ops_telegram_allowed_chat_id,
                    rendered,
                )
            )
```

Keep the exact `settings` construction that `digest.main` already uses; only swap the send call from `send_message` to `send_message_html` and feed it `rendered`.

- [ ] **Step 3: Manual dry run**

Run: `.venv/bin/python -m seedbox_mcp.digest --no-telegram`
Expected: prints a grouped triage report followed by the graduation nudge (if any), no traceback.

- [ ] **Step 4: Commit**

```bash
git add src/seedbox_mcp/digest.py
git commit -m "feat(digest): render daily findings through the triage engine"
```

---

## Task 7: Eval-harness structural assertion

**Files:**

- Modify: `evals/bot_eval.py`
- Test: run the harness.

**Interfaces:**

- Consumes: `parse_findings` from `triage`.

- [ ] **Step 1: Add a triage structural check**

Add a helper in `evals/bot_eval.py` that runs one monitor cycle through the sandbox and asserts structure. Match the file's existing sandbox pattern (read-only tools hit the real system, writes/escalation intercepted, `record_action` patched out). Add:

```python
def check_triage_structure(findings):
    assert isinstance(findings, list)
    assert all(f.severity in ("needs_fix", "watch", "healthy") for f in findings)
    assert all(isinstance(f.real, bool) for f in findings)
    actionable = [f for f in findings if f.severity in ("needs_fix", "watch") and not f.auto_fixed]
    for f in actionable:
        assert f.reason, f"actionable finding {f.id} missing a reason"
    return {"total": len(findings), "actionable": len(actionable)}
```

Wire it into the harness so `--bot ops --triage` (or the existing invocation shape in the file) calls `run_monitor_cycle` under the sandbox and passes the result to `check_triage_structure`, printing the counts.

- [ ] **Step 2: Run the harness**

Run: `.venv/bin/python evals/bot_eval.py --bot ops --triage`
Expected: prints the finding counts and no assertion error. If Ollama-cloud returns a 500 mid-run, re-run before trusting the result (documented harness flakiness).

- [ ] **Step 3: Commit**

```bash
git add evals/bot_eval.py
git commit -m "test(eval): assert monitor cycle returns well-formed structured findings"
```

---

## Self-Review

**Spec coverage:**

- Structured findings + schema + parser: Task 1. Renderer (HTML, no buttons) + fingerprint: Task 2. `send_message_html` with fallback: Task 3. Monitor structured findings + graded auto-fix marked (`_notes_to_findings` from the existing deterministic recoveries): Task 4. On-demand surface: Task 5. Digest adoption: Task 6. Testing + eval harness: Tasks 1-3 unit tests, Task 7 harness. Graded auto-fix reuses the existing deterministic recoveries and graduation gate unchanged (no new autonomy), per the spec's "out of scope."
- Error handling: `parse_findings` fallback (Task 1), `send_message_html` 400 fallback (Task 3), monitor no-alert path preserved (Task 4).

**Placeholder scan:** Task 6 and Task 7 reference "the same settings object main already builds" and "the existing sandbox pattern" rather than pasting code, because those depend on current file contents the implementer will read; every net-new function has complete code. The digest `settings` construction and the eval sandbox harness are existing, read-them-in-place integration points, not new logic.

**Type consistency:** `Finding` fields are used identically across Tasks 2, 4 (`severity`, `title`, `real`, `reason`, `recommendation`, `fixable_by`, `evidence`, `auto_fixed`, `id`). `render_triage` and `fingerprint` signatures match their call sites in Tasks 4, 5, 6. `parse_findings` returns `list[Finding]` consumed in Tasks 4, 5, 6, 7. `run_monitor_cycle` return type change (`str | None` to `list[Finding]`) is reflected at both call sites (Task 4 main, Task 5 on-demand).

**Known integration risk to verify during Task 4/5:** `monitor.py` imports `telegram.send_message_html`, and `telegram_bot.py` imports `monitor.run_monitor_cycle` inside the function body (Task 5) to avoid a load-time cycle. Confirm `python -c "import seedbox_mcp.telegram_bot"` and `import seedbox_mcp.monitor` both succeed.
