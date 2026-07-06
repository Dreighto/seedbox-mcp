from seedbox_mcp.triage import Finding, parse_findings, slugify, render_triage, fingerprint


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
