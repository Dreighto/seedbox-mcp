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
