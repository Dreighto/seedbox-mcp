from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import seedbox_mcp.telegram_bot as bot
from seedbox_mcp import escalations
from seedbox_mcp.triage import Finding, fingerprint, mark_escalated, parse_findings, render_triage


def _f(**kw: Any) -> Finding:
    base: dict[str, Any] = dict(id="x", severity="needs_fix", title="t", real=True, reason="r")
    base.update(kw)
    return Finding(**base)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(escalations, "ESCALATIONS_PATH", tmp_path / "escalations.json")
    return tmp_path


def test_a_reworded_title_with_the_same_key_is_the_same_alert() -> None:
    before = [_f(title="nasdoom_queue: one importblocked item (One Piece S23E25)", key="import-one-piece-s23e25")]
    after = [_f(title="Sonarr import blocked on One Piece S23E25", key="import-one-piece-s23e25")]
    assert fingerprint(before) == fingerprint(after)


def test_the_model_supplied_key_is_parsed() -> None:
    text = json.dumps(
        [{"severity": "watch", "title": "Pool 89% full", "real": True, "reason": "r", "key": "Storage Media Pool"}]
    )
    assert parse_findings(text)[0].issue_key == "storage-media-pool"


def test_escalated_issues_leave_the_fingerprint_and_render_as_in_progress() -> None:
    findings = [_f(title="Import stuck", key="import-x"), _f(title="Disk", severity="healthy")]
    mark_escalated(findings, {"import-x"})
    assert fingerprint(findings) is None
    text, _ = render_triage(findings)
    assert "Import stuck: escalated, a worker is on it" in text
    assert "need your attention" not in text


def test_finished_escalations_are_reported_once_and_dropped(store: Path) -> None:
    escalations.record("a", "Import stuck", "nasops-a", now_ts=0)
    escalations.record("b", "Backup path", "nasops-b", now_ts=0)
    escalations.record("c", "Still running", "nasops-c", now_ts=100)
    (store / "nasops-a.result.json").write_text(json.dumps({"status": "FAILED", "exit_code": 143, "summary": ""}))
    (store / "nasops-b.result.json").write_text(
        json.dumps({"status": "INCONCLUSIVE", "summary": "STATUS: CONFIRMED WORKING\nfixed the mount"})
    )
    messages = escalations.collect_finished(store, now_ts=200)
    assert messages == [
        'The worker you escalated "Import stuck" to did not fix it: it ran out of time before finishing. '
        "If it's still a problem, the next report will flag it again.",
        'The worker you escalated "Backup path" to fixed it: fixed the mount.',
    ]
    assert escalations.active_keys() == {"c"}
    assert escalations.collect_finished(store, now_ts=300) == []


def test_an_escalation_with_no_result_is_given_up_after_six_hours(store: Path) -> None:
    escalations.record("a", "Import stuck", "nasops-a", now_ts=0)
    assert escalations.collect_finished(store, now_ts=escalations.GIVE_UP_AFTER_S - 1) == []
    messages = escalations.collect_finished(store, now_ts=escalations.GIVE_UP_AFTER_S)
    assert messages and "No result came back" in messages[0]
    assert escalations.active_keys() == set()


class _FakeMcp:
    def __init__(self, response: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._response = response

    async def __aenter__(self) -> _FakeMcp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(self._response))])


@pytest.mark.asyncio
async def test_the_escalate_button_records_the_worker_trace(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _FakeMcp({"ok": True, "data": {"escalated": True, "trace_id": "nasops-123"}})
    sent: list[str] = []

    async def fake_send(token: str, chat_id: int, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(bot, "Client", lambda *a, **k: mcp)
    monkeypatch.setattr(bot, "send_message", fake_send)
    finding = _f(title="Import stuck", key="import-x", evidence="One.Piece.S23E25")
    await bot._run_finding_action(bot.BotSettings(), "tok", 1, finding, "esc")
    assert mcp.calls[0][0] == "escalate_to_worker"
    assert "One.Piece.S23E25" in mcp.calls[0][1]["issue"]
    assert escalations.load()["import-x"]["trace_id"] == "nasops-123"
    assert "won't flag this again" in sent[0]


@pytest.mark.asyncio
async def test_a_failed_escalation_is_said_out_loud_and_not_recorded(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _FakeMcp({"ok": False, "message": "Worker dispatch is not configured."})
    sent: list[str] = []

    async def fake_send(token: str, chat_id: int, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(bot, "Client", lambda *a, **k: mcp)
    monkeypatch.setattr(bot, "send_message", fake_send)
    await bot._run_finding_action(bot.BotSettings(), "tok", 1, _f(key="import-x"), "esc")
    assert escalations.load() == {}
    assert sent == ["I couldn't escalate that: Worker dispatch is not configured. It will stay on the reports."]
