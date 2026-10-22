from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.main as blog
from app.db import Incident, make_session_factory
from app.lifecycle import TransitionError, check_transition

USER = {"X-Auth-Request-Email": "oncall@agripulse.example"}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("CSRF_SECRET", "s")
    monkeypatch.setenv("WEBHOOK_TOKEN", "hook")
    blog._sessions = make_session_factory("sqlite://")
    yield
    blog._sessions = None


@pytest.fixture
def client():
    return TestClient(blog.app)


def csrf():
    return blog.csrf_token("oncall@agripulse.example")


def declare(client, title="Mobile uploads failing", severity="SEV2", summary="Farmers cannot upload photos"):
    r = client.post("/incidents", data={"csrf": csrf(), "title": title, "severity": severity, "services": "mobile-backend",
                                        "summary": summary}, headers=USER, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def move(client, iid, to, note="done", review=""):
    return client.post(f"/incidents/{iid}/transition", data={"csrf": csrf(), "to": to, "note": note, "review": review},
                       headers=USER, follow_redirects=False)


# ---------- lifecycle rules ----------
@pytest.mark.parametrize("current, target", [("detected", "contained"), ("triaged", "recovered"), ("closed", "triaged"),
                                             ("detected", "closed")])
def test_cannot_skip_phases(current, target):
    with pytest.raises(TransitionError):
        check_transition(current, target, "note", "review")


def test_operational_incident_may_skip_eradication():
    check_transition("contained", "recovered", "rolled back", None)


def test_note_required_and_review_required_to_close():
    with pytest.raises(TransitionError, match="note"):
        check_transition("detected", "triaged", "  ", None)
    with pytest.raises(TransitionError, match="post-incident review"):
        check_transition("post_incident_review", "closed", "closing", "")


# ---------- the full lifecycle through the web ----------
def test_full_lifecycle_creates_timeline_and_kpis(client):
    iid = declare(client)
    for to in ["triaged", "contained", "eradicated", "recovered", "post_incident_review"]:
        assert move(client, iid, to, note=f"moved to {to}").status_code == 303
    assert move(client, iid, "closed", note="closing").status_code == 409            # review missing
    assert move(client, iid, "closed", note="closing", review="**Cause:** expired cert. Action: alert 30 days before expiry.").status_code == 303
    page = client.get(f"/incidents/{iid}", headers=USER).text
    for label in ["Detected", "Triaged", "Contained", "Eradicated", "Recovered", "Post-incident review", "Closed"]:
        assert label in page
    assert "<strong>Cause:</strong>" in page
    index = client.get("/", headers=USER).text
    assert "Open incidents" in index


def test_invalid_transition_is_409(client):
    iid = declare(client)
    r = move(client, iid, "recovered")
    assert r.status_code == 409 and "cannot move" in r.text


def test_kpis_mtta_and_mttr():
    class I:  # minimal stand-in
        def __init__(self, triaged, recovered, state="closed", severity="SEV2"):
            from datetime import datetime, timezone
            self.detected_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
            self.triaged_at = self.detected_at + timedelta(minutes=triaged) if triaged is not None else None
            self.recovered_at = self.detected_at + timedelta(minutes=recovered) if recovered is not None else None
            self.state, self.severity = state, severity
    k = blog.kpis([I(5, 60), I(15, 120), I(None, None, state="detected")])
    assert k == {"open": 1, "mtta": 10.0, "mttr": 90.0, "total": 3}


def test_markdown_is_sanitised(client):
    iid = declare(client, summary='<script>alert(1)</script> **bold** <img src=x onerror=alert(1)> [x](javascript:alert(1))')
    page = client.get(f"/incidents/{iid}", headers=USER).text
    assert "<script>alert(1)</script>" not in page and "onerror" not in page and "javascript:" not in page
    assert "<strong>bold</strong>" in page
    assert "script-src 'none'" in client.get("/", headers=USER).headers["content-security-policy"]


def test_auth_and_csrf(client):
    assert client.get("/").status_code == 401
    r = client.post("/incidents", data={"csrf": "bad", "title": "Something broke", "severity": "SEV3"}, headers=USER)
    assert r.status_code == 403


def test_alertmanager_opens_one_incident_per_group(client):
    payload = {"status": "firing", "groupKey": "{}:{alertname=\"HighErrorRate\"}",
               "commonLabels": {"alertname": "HighErrorRate", "severity": "critical", "namespace": "agripulse"},
               "alerts": [{"annotations": {"summary": "5xx above 5%"}}]}
    assert client.post("/webhooks/alertmanager", json=payload).status_code == 401
    hdr = {"Authorization": "Bearer hook"}
    first = client.post("/webhooks/alertmanager", json=payload, headers=hdr).json()
    again = client.post("/webhooks/alertmanager", json=payload, headers=hdr).json()
    assert first["created"] and again == {"created": None, "updated": first["created"]}
    with blog.sessions()() as db:
        inc = db.get(Incident, first["created"])
        assert inc.severity == "SEV2" and inc.state == "detected" and inc.commander == "alertmanager"


def test_metrics_report_open_and_untriaged_from_the_database(client):
    declare(client, severity="SEV1")
    iid = declare(client, severity="SEV1")
    move(client, iid, "triaged")
    text = client.get("/metrics").text
    assert 'incidents_open{severity="SEV1"} 2.0' in text
    assert 'incidents_untriaged{severity="SEV1"} 1.0' in text
