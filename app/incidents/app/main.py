"""AgriPulse incidents: the operations team's record of every incident, and its report to Slack.

  GET  /                              all incidents, KPIs (open count, MTTA, MTTR)                  (public)
  POST /incidents                     declare an incident (form, CSRF)            -> Slack report
  GET  /incidents/{id}                the incident and its timeline (blog posts)                    (public)
  POST /incidents/{id}/transition     move to the next lifecycle state with a note -> Slack report when closed
  POST /incidents/{id}/posts          add an update to the timeline without changing state
  POST /webhooks/alertmanager         Alertmanager opens SEV2/SEV3 incidents automatically (Bearer token) -> Slack report
  GET  /healthz /readyz /metrics

Who: anyone may READ the incidents (a public status page); the people who handle them are shown as "operations team".
Declaring, moving and updating an incident needs an AgriPulse account with the "ops" role (granted by an operator in
the accounts service): ops accounts also see the forms and who did what. Everybody else gets 403 on those.
Notes are Markdown, rendered to HTML and then sanitised with an allow-list (no scripts, no event handlers, safe links
only), so a pasted log line cannot inject script into the page.
"""

import hmac
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import bleach
import markdown
from fastapi import Depends, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from markupsafe import Markup
from prometheus_client import Counter, Gauge
from sqlalchemy import select

from agripulse_common import auth, slack, web
from agripulse_common.db import make_session_factory, ping

from .db import Base, Incident, Post
from .lifecycle import ALLOWED, LABELS, SEVERITIES, STATES, TransitionError, check_transition

log = logging.getLogger("agripulse.incidents")
TRANSITIONS = Counter("incident_transitions_total", "Lifecycle transitions", ["to"])
OPEN = Gauge("incidents_open", "Incidents not yet closed", ["severity"])
UNTRIAGED = Gauge("incidents_untriaged", "Incidents still in the Detected state", ["severity"])
ALLOWED_TAGS = ["p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "blockquote", "a", "h3", "h4", "hr", "table",
                "thead", "tbody", "tr", "th", "td"]
SEVERITY_EMOJI = {"SEV1": ":red_circle:", "SEV2": ":large_orange_circle:", "SEV3": ":large_yellow_circle:", "SEV4": ":white_circle:"}
_sessions = None


def sessions():
    global _sessions
    if _sessions is None:
        _sessions = make_session_factory(Base.metadata)
    return _sessions


# Gauges come from the database at scrape time, so every replica reports the same truth
app, templates = web.create_app("incidents", Path(__file__).parent, readiness=lambda: ping(sessions),
                                on_metrics=lambda: refresh_gauges(), loggers=("agripulse", "uvicorn.error"))
ops = auth.requires("ops", html=True)


def is_ops(me: auth.Principal | None) -> bool:
    return me is not None and me.role == "ops"


def utcnow():
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def render(md: str) -> Markup:
    html = markdown.markdown(md or "", extensions=["fenced_code", "tables"])
    clean = bleach.clean(html, tags=ALLOWED_TAGS, attributes={"a": ["href", "title"]}, protocols=["https", "mailto"], strip=True)
    return Markup(bleach.linkify(clean, callbacks=[lambda attrs, new=False: {**attrs, (None, "rel"): "noopener noreferrer"}]))


templates.env.filters["markdown"] = render
templates.env.globals.update(LABELS=LABELS, STATES=STATES, SEVERITIES=SEVERITIES)


# ---------------- Slack ----------------
def incident_url(incident_id: int) -> str:
    return f"{os.environ.get('PUBLIC_URL', '').rstrip('/')}/incidents/{incident_id}"


def report(inc: Incident, headline: str, body: str):
    """The incident report in the operations channel: what, how bad, where, who, and the link to the timeline."""
    env = os.environ.get("ENVIRONMENT", "local")
    prefix = "" if env == "production" else f"[{env}] "
    text = (f"{prefix}{SEVERITY_EMOJI.get(inc.severity, '')} *{headline}: #{inc.id} {inc.title}*\n"
            f"Severity {inc.severity} · services: {inc.services or 'n/a'} · state: {LABELS[inc.state]} · "
            f"commander: {inc.commander} · source: {'Alertmanager' if inc.source != 'manual' else 'declared by a person'}\n"
            f"{body[:1500]}\n<{incident_url(inc.id)}|Open the incident timeline>")
    slack.post(text)


def kpis(incidents: list[Incident]) -> dict:
    """MTTA = detected -> triaged; MTTR = detected -> recovered (minutes, mean over incidents that reached it)."""
    def mean_minutes(attr):
        values = [(aware(getattr(i, attr)) - aware(i.detected_at)).total_seconds() / 60 for i in incidents if getattr(i, attr)]
        return round(sum(values) / len(values), 1) if values else None
    return {"open": sum(1 for i in incidents if i.state != "closed"), "mtta": mean_minutes("triaged_at"),
            "mttr": mean_minutes("recovered_at"), "total": len(incidents)}


def create_incident(db, title: str, severity: str, services: str, summary: str, author: str, source: str = "manual") -> Incident:
    inc = Incident(title=title.strip(), severity=severity, services=services.strip(), state="detected",
                   commander=author, detected_at=utcnow(), source=source)
    db.add(inc)
    db.flush()
    db.add(Post(incident_id=inc.id, author=author, state_to="detected", body=summary or "Incident declared."))
    TRANSITIONS.labels("detected").inc()
    return inc


def refresh_gauges():
    with sessions()() as db:
        rows = db.execute(select(Incident.severity, Incident.state)).all()
    for sev in SEVERITIES:
        OPEN.labels(sev).set(sum(1 for s, st in rows if s == sev and st != "closed"))
        UNTRIAGED.labels(sev).set(sum(1 for s, st in rows if s == sev and st == "detected"))


# ---------------- pages ----------------
@app.get("/")
def index(request: Request, me: auth.Principal | None = Depends(auth.optional)):
    with sessions()() as db:
        incidents = db.scalars(select(Incident).order_by(Incident.detected_at.desc())).all()
    return web.page(templates, request, "index.html", {"me": me, "is_ops": is_ops(me), "incidents": incidents,
                                                       "kpi": kpis(incidents), "login": auth.login_url(request)})


@app.post("/incidents")
def declare(request: Request, me: auth.Principal = Depends(ops), csrf: str = Form(...),
            title: str = Form(..., min_length=5, max_length=140), severity: str = Form(..., pattern=r"^SEV[1-4]$"),
            services: str = Form("", max_length=200), summary: str = Form("", max_length=10_000)):
    web.check_csrf(request, csrf)
    with sessions()() as db:
        inc = create_incident(db, title, severity, services, summary, me.email)
        db.commit()
        log.info("incident %s declared (%s)", inc.id, severity)
        report(inc, "New incident", summary or "Incident declared.")
        return RedirectResponse(f"/incidents/{inc.id}", status_code=303)


@app.get("/incidents/{incident_id}")
def incident_page(request: Request, incident_id: int, me: auth.Principal | None = Depends(auth.optional)):
    with sessions()() as db:
        inc = db.get(Incident, incident_id)
        if not inc:
            raise HTTPException(status_code=404, detail="incident not found")
        posts = db.scalars(select(Post).where(Post.incident_id == incident_id).order_by(Post.at, Post.id)).all()
    return web.page(templates, request, "incident.html", {"me": me, "is_ops": is_ops(me), "inc": inc, "posts": posts,
                                                         "next_states": sorted(ALLOWED[inc.state], key=STATES.index)})


@app.post("/incidents/{incident_id}/transition")
def transition(request: Request, incident_id: int, me: auth.Principal = Depends(ops), csrf: str = Form(...),
               to: str = Form(...), note: str = Form("", max_length=10_000), review: str = Form("", max_length=20_000)):
    web.check_csrf(request, csrf)
    with sessions()() as db:
        inc = db.get(Incident, incident_id)
        if not inc:
            raise HTTPException(status_code=404, detail="incident not found")
        try:
            check_transition(inc.state, to, note, review or inc.review)
        except TransitionError as e:
            raise HTTPException(status_code=409, detail=str(e))
        db.add(Post(incident_id=inc.id, author=me.email, state_from=inc.state, state_to=to, body=note))
        inc.state = to
        stamp = utcnow()
        if to == "triaged" and not inc.triaged_at:
            inc.triaged_at = stamp
        if to == "recovered":
            inc.recovered_at = stamp
        if to == "closed":
            inc.closed_at = stamp
        if review:
            inc.review = review
        db.commit()
        if to in ("recovered", "closed"):
            minutes = round((aware(inc.recovered_at or stamp) - aware(inc.detected_at)).total_seconds() / 60)
            report(inc, "Incident " + LABELS[to].lower(), f"Recovered after {minutes} min. {note}")
    TRANSITIONS.labels(to).inc()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@app.post("/incidents/{incident_id}/posts")
def add_post(request: Request, incident_id: int, me: auth.Principal = Depends(ops), csrf: str = Form(...),
             body: str = Form(..., min_length=1, max_length=10_000)):
    web.check_csrf(request, csrf)
    with sessions()() as db:
        if not db.get(Incident, incident_id):
            raise HTTPException(status_code=404, detail="incident not found")
        db.add(Post(incident_id=incident_id, author=me.email, body=body))
        db.commit()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@app.post("/webhooks/alertmanager", status_code=202)
async def alertmanager(request: Request, authorization: str | None = Header(default=None)):
    """Firing critical/warning alerts open an incident (one per alert group, deduplicated) and report it to Slack."""
    expected = os.environ.get("WEBHOOK_TOKEN", "")
    scheme, _, token = (authorization or "").partition(" ")
    if not expected or scheme != "Bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorised")
    payload = await request.json()
    if payload.get("status") != "firing":
        return {"created": None}
    labels = payload.get("commonLabels", {})
    group = payload.get("groupKey", "")[:200]
    severity = {"critical": "SEV2", "warning": "SEV3"}.get(labels.get("severity"), "SEV4")
    title = f'{labels.get("alertname", "Alert")} ({labels.get("namespace", "cluster")})'[:140]
    summary = "\n".join(f'- {a.get("annotations", {}).get("summary", "")}' for a in payload.get("alerts", [])[:10])
    with sessions()() as db:
        existing = db.scalars(select(Incident).where(Incident.source == group, Incident.state != "closed")).first()
        if existing:
            db.add(Post(incident_id=existing.id, author="alertmanager", body=f"Still firing:\n{summary}"))
            db.commit()
            return {"created": None, "updated": existing.id}
        inc = create_incident(db, title, severity, labels.get("namespace", ""),
                              f"Opened automatically by Alertmanager.\n\n{summary}", "alertmanager", source=group)
        db.commit()
        report(inc, "New incident (from an alert)", summary)
        return {"created": inc.id}
