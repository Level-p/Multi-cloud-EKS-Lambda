"""AgriPulse incident blog: the incident-response team's record of every incident (namespace incident-response).

  GET  /                              all incidents, KPIs (open count, MTTA, MTTR)
  POST /incidents                     declare an incident (form, CSRF)
  GET  /incidents/{id}                the incident and its timeline (blog posts)
  POST /incidents/{id}/transition     move to the next lifecycle state with a note (form, CSRF)
  POST /incidents/{id}/posts          add an update to the timeline without changing state
  POST /webhooks/alertmanager         Alertmanager opens SEV2/SEV3 incidents automatically (Bearer token)
  GET  /healthz /readyz /metrics

Sign-in: oauth2-proxy (Cognito staff user pool) in front sets X-Auth-Request-Email.
Notes are Markdown, rendered to HTML and then sanitised with an allow-list (no scripts, no
event handlers, safe links only), so a pasted log line cannot inject script into the page.
"""

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import bleach
import markdown
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import select, text

from . import telemetry
from .db import Incident, Post, make_session_factory
from .lifecycle import ALLOWED, LABELS, SEVERITIES, STATES, TransitionError, check_transition

logging.basicConfig(level=logging.INFO, format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}')
log = logging.getLogger("incident-blog")

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request duration", ["method", "route"])
TRANSITIONS = Counter("incident_transitions_total", "Lifecycle transitions", ["to"])
OPEN = Gauge("incidents_open", "Incidents not yet closed", ["severity"])
UNTRIAGED = Gauge("incidents_untriaged", "Incidents still in the Detected state", ["severity"])

app = FastAPI(title="AgriPulse incident blog", docs_url=None, redoc_url=None, openapi_url=None)
telemetry.setup(app, loggers=("incident-blog", "uvicorn.error"))   # OpenTelemetry; off without OTEL_EXPORTER_OTLP_ENDPOINT
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
_sessions = None

ALLOWED_TAGS = ["p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "blockquote", "a", "h3", "h4", "hr", "table",
                "thead", "tbody", "tr", "th", "td"]
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'none'; img-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
}


def sessions():
    global _sessions
    if _sessions is None:
        _sessions = make_session_factory()
    return _sessions


def utcnow():
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def render(md: str) -> Markup:
    html = markdown.markdown(md or "", extensions=["fenced_code", "tables"])
    clean = bleach.clean(html, tags=ALLOWED_TAGS, attributes={"a": ["href", "title"]},
                         protocols=["https", "mailto"], strip=True)
    return Markup(bleach.linkify(clean, callbacks=[lambda attrs, new=False: {**attrs, (None, "rel"): "noopener noreferrer"}]))


templates.env.filters["markdown"] = render
templates.env.globals.update(LABELS=LABELS, STATES=STATES, SEVERITIES=SEVERITIES)


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    route = getattr(request.scope.get("route"), "path", "unmatched")
    REQUESTS.labels(request.method, route, str(response.status_code)).inc()
    LATENCY.labels(request.method, route).observe(time.perf_counter() - start)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


def current_user(x_forwarded_email: str | None = Header(default=None),
                 x_auth_request_email: str | None = Header(default=None)) -> str:
    # Set by oauth2-proxy after Cognito sign-in; the NetworkPolicy lets only oauth2-proxy reach this pod
    email = x_forwarded_email or x_auth_request_email or os.environ.get("DEV_USER")
    if not email:
        raise HTTPException(status_code=401, detail="sign in required")
    return email.lower()


def csrf_token(user: str) -> str:
    return hmac.new(os.environ.get("CSRF_SECRET", "").encode(), user.encode(), hashlib.sha256).hexdigest()


def check_csrf(user: str, token: str):
    if not os.environ.get("CSRF_SECRET") or not hmac.compare_digest(token, csrf_token(user)):
        raise HTTPException(status_code=403, detail="invalid form token, reload the page")


def kpis(incidents: list[Incident]) -> dict:
    """MTTA = detected -> triaged; MTTR = detected -> recovered (minutes, mean over incidents that reached it)."""
    def mean_minutes(attr):
        values = [(aware(getattr(i, attr)) - aware(i.detected_at)).total_seconds() / 60 for i in incidents if getattr(i, attr)]
        return round(sum(values) / len(values), 1) if values else None
    open_incidents = [i for i in incidents if i.state != "closed"]
    return {"open": len(open_incidents), "mtta": mean_minutes("triaged_at"), "mttr": mean_minutes("recovered_at"),
            "total": len(incidents)}


def create_incident(db, title: str, severity: str, services: str, summary: str, author: str, source: str = "manual") -> Incident:
    inc = Incident(title=title.strip(), severity=severity, services=services.strip(), state="detected",
                   commander=author, detected_at=utcnow(), source=source)
    db.add(inc)
    db.flush()
    db.add(Post(incident_id=inc.id, author=author, state_to="detected", body=summary or "Incident declared."))
    TRANSITIONS.labels("detected").inc()
    return inc


# ---------------- pages ----------------
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        with sessions()() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")


def refresh_gauges():
    """Gauges come from the database at scrape time, so every replica reports the same truth."""
    with sessions()() as db:
        rows = db.execute(select(Incident.severity, Incident.state)).all()
    for sev in SEVERITIES:
        OPEN.labels(sev).set(sum(1 for s, st in rows if s == sev and st != "closed"))
        UNTRIAGED.labels(sev).set(sum(1 for s, st in rows if s == sev and st == "detected"))


@app.get("/metrics")
def metrics():
    try:
        refresh_gauges()
    except Exception:
        log.exception("could not refresh incident gauges")
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Depends(current_user)):
    with sessions()() as db:
        incidents = db.scalars(select(Incident).order_by(Incident.detected_at.desc())).all()
    return templates.TemplateResponse(request, "index.html", {"user": user, "incidents": incidents, "kpi": kpis(incidents),
                                                              "csrf": csrf_token(user)})


@app.post("/incidents")
def declare(user: str = Depends(current_user), csrf: str = Form(...), title: str = Form(..., min_length=5, max_length=140),
            severity: str = Form(..., pattern=r"^SEV[1-4]$"), services: str = Form("", max_length=200),
            summary: str = Form("", max_length=10_000)):
    check_csrf(user, csrf)
    with sessions()() as db:
        inc = create_incident(db, title, severity, services, summary, user)
        db.commit()
        log.info("incident %s declared by %s (%s)", inc.id, user, severity)
        return RedirectResponse(f"/incidents/{inc.id}", status_code=303)


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
def incident_page(request: Request, incident_id: int, user: str = Depends(current_user)):
    with sessions()() as db:
        inc = db.get(Incident, incident_id)
        if not inc:
            raise HTTPException(status_code=404, detail="incident not found")
        posts = db.scalars(select(Post).where(Post.incident_id == incident_id).order_by(Post.at, Post.id)).all()
    return templates.TemplateResponse(request, "incident.html", {
        "user": user, "inc": inc, "posts": posts, "csrf": csrf_token(user),
        "next_states": sorted(ALLOWED[inc.state], key=STATES.index)})


@app.post("/incidents/{incident_id}/transition")
def transition(incident_id: int, user: str = Depends(current_user), csrf: str = Form(...),
               to: str = Form(...), note: str = Form("", max_length=10_000), review: str = Form("", max_length=20_000)):
    check_csrf(user, csrf)
    with sessions()() as db:
        inc = db.get(Incident, incident_id)
        if not inc:
            raise HTTPException(status_code=404, detail="incident not found")
        try:
            check_transition(inc.state, to, note, review or inc.review)
        except TransitionError as e:
            raise HTTPException(status_code=409, detail=str(e))
        db.add(Post(incident_id=inc.id, author=user, state_from=inc.state, state_to=to, body=note))
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
    TRANSITIONS.labels(to).inc()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@app.post("/incidents/{incident_id}/posts")
def add_post(incident_id: int, user: str = Depends(current_user), csrf: str = Form(...), body: str = Form(..., min_length=1, max_length=10_000)):
    check_csrf(user, csrf)
    with sessions()() as db:
        inc = db.get(Incident, incident_id)
        if not inc:
            raise HTTPException(status_code=404, detail="incident not found")
        db.add(Post(incident_id=incident_id, author=user, body=body))
        db.commit()
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@app.post("/webhooks/alertmanager", status_code=202)
async def alertmanager(request: Request, authorization: str | None = Header(default=None)):
    """Firing critical/warning alerts open an incident (one per alert group, deduplicated)."""
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
        inc = create_incident(db, title, severity, labels.get("namespace", ""), f"Opened automatically by Alertmanager.\n\n{summary}",
                              "alertmanager", source=group)
        db.commit()
        return {"created": inc.id}
