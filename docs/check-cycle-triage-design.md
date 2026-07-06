# Check-Cycle Triage Surface, Design

Date: 2026-07-05
Status: Approved design, Phase 1 to build first
Applies to: the operator bot (`@nas_doombot`, `telegram_bot.py`), the scheduled
`monitor` and `digest`, and later the friend bot (`@nasdoom_helperbot`).

## Problem

The whole-NAS check cycle currently comes back as a wall of free-form text the
model hand-formats. The operator cannot see at a glance what is healthy versus
what needs fixing, and there is no explicit "is this a real problem or a false
alarm" call, nor a clean path from "found a problem" to "fixed it". As the bot
took on the whole NAS (not just content queries), this report became the main
thing the operator reads, and its formatting has not kept up.

## Goal

Turn the check cycle into a scannable triage surface:

1. See at a glance what is going on and what needs attention.
2. The model diagnoses each finding and calls it real or a false alarm, with a
   reason.
3. For a real problem the bot either fixes it (only the safe, already-proven
   actions, on its own) or surfaces it with a recommended action, and later a
   one-tap fix or an auto-dispatched fix agent.

## Core idea

Stop letting the model hand-format a wall of text. The model returns
**structured findings**, and code owns the two parts that must be reliable:
formatting and action routing. This is the same "deterministic guard over
prompt hope" pattern the codebase already uses for the confirm gate, the punt
guard, the em-dash strip, and the search-year fix.

Each finding carries:

- `severity`: `needs_fix` | `watch` | `healthy`
- `title`: one short line
- `verdict`: `{ real: bool, reason: str }` (the real-vs-false-alarm call)
- `action`: `{ recommendation: str, fixable_by: "proven" | "tap" | "agent" | "none" }`
- `evidence`: optional short string for IDs, paths, sizes, quoted tool output
- `id`: stable slug used to route a later button tap to the right fix

`fixable_by` meanings:

- `proven`: a graduated, deterministic recovery the bot already runs unattended.
- `tap`: the bot has a tool that can fix it, but it is not graduated, so it
  waits for the operator (a tap in Phase 2, a text recommendation in Phase 1).
- `agent`: needs a code or deep-investigation change, hand to a fix agent.
- `none`: informational, just watch.

## Architecture

One shared engine, two surfaces (scheduled push and on-demand), so the report
looks and behaves the same everywhere.

### New module: `triage.py`

- `Finding` (dataclass) and `TRIAGE_SCHEMA` (JSON Schema for a list of findings).
  The model is prompted to return findings as JSON matching the schema instead
  of prose. Output is parsed and validated; invalid output falls back safely
  (see Error handling).
- `render_triage(findings, *, interactive: bool) -> (text, reply_markup | None)`:
  a pure function that groups findings by severity and produces the Telegram
  message. Rendered as HTML parse mode, which the code fully controls, so a
  stray character in model text can never break the whole message. In Phase 1
  `interactive` is always `False` and `reply_markup` is always `None`.
- `fingerprint(findings) -> str`: stable hash over the set of
  `(severity, title)` for the alert dedup, replacing the current raw-text
  `sha256` in `monitor.main`.
- `run_triage_cycle(model, *, source) -> list[Finding]`: shared driver that runs
  the checklist, runs the graded auto-fix (below), and returns findings. Used by
  the monitor, the on-demand command, and later the digest.

### `telegram.py`

Add `send_message_html(token, chat_id, text, reply_markup=None)`: an HTML
parse-mode send with a plain-text fallback on a 400, keeping the existing no-slop
dash strip and the 4000-char guard. The existing `send_message` (legacy Markdown)
stays for the model's free-form replies, which are not code-controlled. The
triage renderer escapes all model-supplied text and uses `<code>` / `<pre>` for
evidence.

### Graded auto-fix

`run_triage_cycle` runs the existing deterministic recoveries
(`_deterministic_queue_resume`, `_deterministic_service_recovery`) and the
graduation gate before rendering. Anything auto-fixed becomes a finding marked
`auto_fixed = True` and rendered with a check mark under a short "auto-fixed this
cycle" group. Nothing that is not graduated is ever fixed unattended. This keeps
the graduation-gate discipline exactly as it is today.

### Surfaces

- **Scheduled monitor** (`monitor.py`): `run_monitor_cycle` returns
  `list[Finding]` instead of text. `main` renders with `render_triage`,
  fingerprints for dedup, and pushes with `send_message_html`. The persisted
  alert text in `.monitor_alert_state.json` (read by the interactive bot to
  resolve "investigate it") becomes the rendered report.
- **On-demand** (`telegram_bot.py`): a new intent ("full status", "run the
  checks", "status report") runs the same `run_triage_cycle` and renders the
  same way.
- **Digest** (`digest.py`): adopts `render_triage` for its findings section so
  the daily report matches. The graduation nudge stays appended. Optional for
  Phase 1, low risk to include.

## Phase 1, the scannable triage report (build first)

Scope:

- `triage.py`: `Finding`, `TRIAGE_SCHEMA`, `render_triage` (no buttons),
  `fingerprint`, `run_triage_cycle` with graded auto-fix.
- `telegram.py`: `send_message_html`.
- `monitor.py`: produce and render structured findings, dedup on the new
  fingerprint.
- `telegram_bot.py`: the on-demand "full status" intent.
- `digest.py`: render its findings through `render_triage`.
- Actions are shown as text recommendations. No buttons yet.

Example rendered report:

```
NAS OPS, 2 need attention

NEEDS FIX (1)
- Sonarr import stuck: The Irishman
  real. fix_import can resolve it (not yet graduated).
  recommend: ask me to fix it, or send it to an agent.

WATCH (1)
- 2 requests waiting on release (Deep Water, Disclosure Day)
  real, not actionable yet.

AUTO-FIXED THIS CYCLE (1)
- Sonarr queue was stalled 2h, resumed (proven action)

HEALTHY (12): disks, fleet, adguard, tdarr, jellyseerr, ...
```

Phase 1 gives the at-a-glance win on both surfaces without any new interaction
model.

## Phase 2, tap-to-act (fast follow)

Port the friend bot's callback plumbing into the operator bot: `reply_markup`
with `inline_keyboard`, `_answer_callback` (answerCallbackQuery),
`_edit_message_text` (editMessageText), and the `callback_query` branch in the
run loop. `telegram_bot.py` has none of this today; the friend bot has all of it
already, so this is a port, not new work.

- `render_triage(interactive=True)` adds buttons to each actionable finding:
  `[Fix it now]` (for `tap`), `[Send to agent]`, `[Ignore]`.
- A per-message store maps `finding.id` to the concrete fix (tool name plus
  args), so a later tap knows exactly what to run. Same idea as the friend bot's
  tracking store.
- Callback executor:
  - `[Fix it now]`: run the mapped tool inline, edit the message to show the
    result.
  - `[Send to agent]`: generate the fix prompt, auto-dispatch a headless Claude
    agent (fix on a branch, run tests, do not deploy), show the prompt, and add
    an `[approve deploy]` button. Reuses the `friend_error_watch.py` dispatch
    pattern.
  - `[Ignore]`: edit the message to dismiss the finding.
- Callback data encodes `finding.id` plus the action. Stale or expired taps
  (finding no longer in the store) are answered gracefully.

## Error handling

- Model returns malformed or non-conforming JSON: fall back to a plain
  "could not structure the checks this cycle" note plus the raw model text, so a
  cycle is never lost.
- HTML render escapes all model-supplied text.
- `send_message_html` falls back to plain text on a Telegram 400, same as the
  current send path.
- Phase 2 callbacks validate `finding.id` against the per-message store before
  acting.

## Testing

- Pure functions get unit tests: `render_triage` (grouping, escaping, evidence),
  `fingerprint` (stable, order-independent), and the finding classification and
  auto-fix marking.
- Extend `evals/bot_eval.py` to drive a triage cycle and assert the structure
  (severities present, verdicts present, auto-fixed marked), with writes and
  escalation intercepted as the harness already does.
- The graduation ledger must not be polluted by sandbox runs, same guard as the
  existing eval harness.

## Out of scope

- No change to the graduation gate or to which actions are graduated.
- No new autonomous fixes. The graded posture is unchanged: proven actions run
  unattended, everything else waits for the operator.
- The friend bot inherits Phase 1 rendering later, once it is proven on the
  operator bot.
