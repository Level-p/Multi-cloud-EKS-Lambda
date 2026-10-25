import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.main as blog
from agripulse_common import slack
from agripulse_common.testing import token
from app.db import Incident
from app.lifecycle import TransitionError, check_transition

REAL_POST = slack.post   # the autouse fixture below replaces it


@pytest.fixture(autouse=True)
def reports(monkeypatch):
    """Slack reports, captured instead of sent."""
    sent = []
    monkeypatch.setattr(slack, "post", lambda text, blocks=None, timeout=5.0: sent.append(text) or True)
    blog._sessions = None
    yield sent
    blog._sessions = None


@pytest.fixture
def client():
    c = TestClient(blog.app)
    c.cookies.set("agripulse_session", token("ops", email="oncall@agripulse.example", name="On-call"))
    return c


def csrf(client):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/").text).group(1)


def declare(client, title="Mobile uploads failing", severity="SEV2", summary="Farmers cannot upload photos"):
    r = client.post("/incidents", data={"csrf": csrf(client), "title": title, "severity": severity, "services": "mobile-backend",
                                        "summary": summary}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def move(client, iid, to, note="done", review=""):
    return client.post(f"/incidents/{iid}/transition", data={"csrf": csrf(client), "to": to, "note": note, "review": review},
                       follow_redirects=False)


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
    page = client.get(f"/incidents/{iid}").text
    for label in ["Detected", "Triaged", "Contained", "Eradicated", "Recovered", "Post-incident review", "Closed"]:
        assert label in page
    assert "<strong>Cause:</strong>" in page
    index = client.get("/").text
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
    page = client.get(f"/incidents/{iid}").text
    assert "<script>alert(1)</script>" not in page and "onerror" not in page and "javascript:" not in page
    assert "<strong>bold</strong>" in page
    assert "script-src 'none'" in client.get("/").headers["content-security-policy"]


def test_anyone_reads_only_ops_writes_and_forms_need_the_token(client):
    iid = declare(client)
    anonymous = TestClient(blog.app)
    for path in ("/", f"/incidents/{iid}"):
        page = anonymous.get(path)
        assert page.status_code == 200 and "Mobile uploads failing" in page.text
        assert "operations team" in page.text and "@" not in page.text.split("<footer")[0].split("</header>")[-1]
        assert 'action="/incidents' not in page.text            # no forms for the public
    assert anonymous.post("/incidents", data={"csrf": "x", "title": "Something broke", "severity": "SEV3"},
                          follow_redirects=False).headers["location"].startswith("http://accounts.test/login?next=")
    buyer = TestClient(blog.app)
    buyer.cookies.set("agripulse_session", token("buyer"))
    assert buyer.get("/").status_code == 200 and 'action="/incidents"' not in buyer.get("/").text
    assert buyer.post("/incidents", data={"csrf": "x", "title": "Something broke", "severity": "SEV3"}).status_code == 403
    assert buyer.post(f"/incidents/{iid}/posts", data={"csrf": "x", "body": "hi"}).status_code == 403
    assert 'action="/incidents"' in client.get("/").text            # ops see the forms
    r = client.post("/incidents", data={"csrf": "bad", "title": "Something broke", "severity": "SEV3"})
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


def test_every_new_incident_and_its_recovery_are_reported_to_slack(client, reports):
    iid = declare(client, title="Checkout failing", severity="SEV1", summary="Buyers cannot place orders")
    assert len(reports) == 1 and "New incident: #%d Checkout failing" % iid in reports[0]
    assert "SEV1" in reports[0] and "Buyers cannot place orders" in reports[0] and f"/incidents/{iid}|" in reports[0]
    for to in ["triaged", "contained", "recovered"]:
        move(client, iid, to, note=f"moved to {to}")
    assert len(reports) == 2 and "Incident recovered" in reports[1] and "Recovered after" in reports[1]


def test_an_alert_incident_is_reported_once(client, reports):
    payload = {"status": "firing", "groupKey": "g1", "commonLabels": {"alertname": "ShopErrors", "severity": "warning",
                                                                       "namespace": "shop-production"},
               "alerts": [{"annotations": {"summary": "5xx above 5%"}}]}
    for _ in range(2):
        client.post("/webhooks/alertmanager", json=payload, headers={"Authorization": "Bearer hook"})
    assert len(reports) == 1 and "from an alert" in reports[0] and "ShopErrors (shop-production)" in reports[0]


def test_slack_failure_never_blocks_recording(monkeypatch):
    import urllib.request

    def boom(*a, **kw):
        raise OSError("slack is down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert REAL_POST("x") is False   # logged and counted, not raised
