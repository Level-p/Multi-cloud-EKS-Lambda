"""Tests for app/dast/zap_gate.py (python -m pytest -q app/dast/tests)."""
import json
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import zap_gate  # noqa: E402

TODAY = "2026-10-18"


def alert(plugin, risk, uris=("http://t/api/x",), confidence=2, name=None):
    return {"pluginid": str(plugin), "alert": name or f"rule {plugin}", "riskcode": str(risk), "confidence": str(confidence),
            "cweid": "79", "solution": "<p>Fix it</p>", "count": str(len(uris)),
            "instances": [{"uri": u, "method": "GET"} for u in uris]}


def write(tmp_path, alerts, exceptions=(), fail_on="medium"):
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"@version": "2.17.0", "site": [{"@name": "http://t", "alerts": list(alerts)}]}))
    policy = tmp_path / "p.json"
    policy.write_text(json.dumps({"fail_on": fail_on, "exceptions": list(exceptions)}))
    return str(report), str(policy)


def run(tmp_path, alerts, exceptions=(), extra=(), fail_on="medium"):
    report, policy = write(tmp_path, alerts, exceptions, fail_on)
    return zap_gate.main([report, "--policy", policy, "--today", TODAY, *extra])


def exc(plugin, expires="2027-01-31", **kw):
    return {"plugin_id": str(plugin), "reason": "reviewed", "owner": "security", "expires": expires, **kw}


def test_no_findings_passes(tmp_path):
    assert run(tmp_path, []) == 0


def test_low_findings_pass_a_medium_gate(tmp_path):
    assert run(tmp_path, [alert(10021, 1), alert(10036, 0)]) == 0


def test_medium_finding_fails(tmp_path, capsys):
    assert run(tmp_path, [alert(10038, 2, name="CSP Header Not Set")]) == 1
    assert "CSP Header Not Set" in capsys.readouterr().out


def test_high_gate_lets_medium_through(tmp_path):
    assert run(tmp_path, [alert(10038, 2)], extra=["--fail-on", "high"]) == 0
    assert run(tmp_path, [alert(40018, 3)], extra=["--fail-on", "high"]) == 1


def test_false_positive_confidence_zero_is_ignored(tmp_path):
    assert run(tmp_path, [alert(40018, 3, confidence=0)]) == 0


def test_valid_exception_accepts_the_risk(tmp_path, capsys):
    assert run(tmp_path, [alert(10038, 2)], [exc(10038)]) == 0
    assert "Accepted risk" in capsys.readouterr().out


def test_expired_exception_fails_again(tmp_path, capsys):
    assert run(tmp_path, [alert(10038, 2)], [exc(10038, expires="2026-10-01")]) == 1
    assert "Expired exceptions" in capsys.readouterr().out


def test_exception_limited_by_uri_regex(tmp_path):
    ex = [exc(10038, uri_regex=r"/static/")]
    assert run(tmp_path, [alert(10038, 2, uris=["http://t/static/a.css"])], ex) == 0
    # one instance outside the accepted scope: the whole alert still fails
    assert run(tmp_path, [alert(10038, 2, uris=["http://t/static/a.css", "http://t/api/x"])], ex) == 1


def test_unused_exceptions_are_reported(tmp_path, capsys):
    assert run(tmp_path, [], [exc(99999)]) == 0
    assert "Unused exceptions" in capsys.readouterr().out


def test_policy_errors_are_exit_2(tmp_path):
    assert run(tmp_path, [], [{"plugin_id": "1", "reason": "", "owner": "x", "expires": "2027-01-01"}]) == 2
    assert run(tmp_path, [], [exc(1, expires="2030-01-01")]) == 2           # more than 180 days ahead
    assert run(tmp_path, [], [exc(1, expires="31/01/2027")]) == 2
    assert run(tmp_path, [], [exc(1, uri_regex="(")]) == 2


def test_missing_or_wrong_report_is_exit_2(tmp_path):
    policy = tmp_path / "p.json"
    policy.write_text("{}")
    assert zap_gate.main([str(tmp_path / "missing.json"), "--policy", str(policy)]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "zap"}')
    assert zap_gate.main([str(bad), "--policy", str(policy)]) == 2


def test_junit_and_summary_files(tmp_path):
    summary, junit = tmp_path / "s.md", tmp_path / "j.xml"
    rc = run(tmp_path, [alert(10038, 2), alert(10020, 2), alert(10021, 1)], [exc(10020)],
             extra=["--summary", str(summary), "--junit", str(junit)])
    assert rc == 1
    suite = ET.parse(junit).getroot()
    assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("3", "1", "1")
    assert "FAILED" in summary.read_text()
