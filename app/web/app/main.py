"""AgriPulse web: farm management dashboard for agronomists and co-operatives (EKS).

  GET  /                        dashboard: market prices + farms with total harvest   (cached)
  POST /farms                   add a farm                                          (form, CSRF)
  GET  /farms/{id}              farm detail with harvests                           (cached)
  POST /farms/{id}/harvests     record a harvest                                    (form, CSRF)
  GET  /api/prices?crop=maize   latest market prices (JSON)                         (cached)
  POST /api/prices              publish a price (price admins only)
  GET  /healthz /readyz /metrics

Who is calling: oauth2-proxy (Cognito staff sign-in) sits in front and sets X-Auth-Request-Email;
a NetworkPolicy lets only oauth2-proxy reach this pod, so the header cannot be forged.
Edge protection (bots, SQL injection, rate limits) is AWS WAF on CloudFront; the app still
validates everything and only uses bound SQL parameters.
"""

import hashlib
import hmac
import logging
import os
import re
import time
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from . import telemetry
from .cache import Cache, make_client
from .db import Farm, Harvest, MarketPrice, make_session_factory

logging.basicConfig(level=logging.INFO, format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}')
log = logging.getLogger("agripulse.web")

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request duration", ["method", "route"],
                    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))

app = FastAPI(title="AgriPulse web", docs_url=None, redoc_url=None, openapi_url=None)
telemetry.setup(app, loggers=("agripulse", "uvicorn.error"), redis=True)   # OpenTelemetry; off without OTEL_EXPORTER_OTLP_ENDPOINT
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))  # autoescape on for .html
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
CROP_PATTERN = r"^[A-Za-z][A-Za-z \-]{1,39}$"
_sessions = None
_cache = None


def sessions():
    global _sessions
    if _sessions is None:
        _sessions = make_session_factory()
    return _sessions


def cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache(make_client())
    return _cache


# ---------------- security plumbing ----------------
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin", "Strict-Transport-Security": "max-age=31536000",
}


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


def is_price_admin(user: str) -> bool:
    admins = {a.strip().lower() for a in os.environ.get("PRICE_ADMINS", "").split(",") if a.strip()}
    return user in admins


# ---------------- data access (cached) ----------------
def latest_prices(crop: str | None = None) -> list[dict]:
    def load():
        with sessions()() as db:
            q = select(MarketPrice).order_by(MarketPrice.on.desc(), MarketPrice.crop).limit(50)
            if crop:
                q = q.where(MarketPrice.crop == crop)
            return [{"crop": p.crop, "market": p.market, "price_per_tonne": p.price_per_tonne,
                     "currency": p.currency, "on": p.on.isoformat()} for p in db.scalars(q)]
    return cache().get_or_load(f"prices:{crop or 'all'}", 300, load, "prices")


def farms_overview() -> list[dict]:
    def load():
        with sessions()() as db:
            rows = db.execute(select(Farm, func.coalesce(func.sum(Harvest.yield_tonnes), 0.0))
                              .outerjoin(Harvest, Harvest.farm_id == Farm.id).group_by(Farm.id).order_by(Farm.name)).all()
            return [{"id": f.id, "name": f.name, "region": f.region, "hectares": f.hectares, "total_tonnes": round(t, 2)}
                    for f, t in rows]
    return cache().get_or_load("farms:overview", 120, load, "farms")


def farm_detail(farm_id: int) -> dict | None:
    def load():
        with sessions()() as db:
            farm = db.get(Farm, farm_id)
            if not farm:
                return None
            hs = db.scalars(select(Harvest).where(Harvest.farm_id == farm_id).order_by(Harvest.recorded_at.desc())).all()
            return {"id": farm.id, "name": farm.name, "region": farm.region, "hectares": farm.hectares,
                    "harvests": [{"crop": h.crop, "season": h.season, "yield_tonnes": h.yield_tonnes,
                                  "per_hectare": round(h.yield_tonnes / farm.hectares, 2) if farm.hectares else None,
                                  "recorded_by": h.recorded_by} for h in hs]}
    return cache().get_or_load(f"farm:{farm_id}", 120, load, "farm")


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
        log.exception("database not reachable")
        raise HTTPException(status_code=503, detail="database unavailable")


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(current_user)):
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "prices": latest_prices(), "farms": farms_overview(), "csrf": csrf_token(user)})


@app.post("/farms")
def create_farm(user: str = Depends(current_user), csrf: str = Form(...),
                name: str = Form(..., min_length=2, max_length=100),
                region: str = Form(..., min_length=2, max_length=60, pattern=r"^[A-Za-z][A-Za-z \-]+$"),
                hectares: float = Form(..., gt=0, le=1_000_000)):
    check_csrf(user, csrf)
    with sessions()() as db:
        farm = Farm(name=name.strip(), region=region.strip(), hectares=hectares)
        db.add(farm)
        db.commit()
    cache().invalidate("farms:overview")
    log.info("farm created by %s", user)
    return RedirectResponse(f"/farms/{farm.id}", status_code=303)


@app.get("/farms/{farm_id}", response_class=HTMLResponse)
def farm_page(request: Request, farm_id: int, user: str = Depends(current_user)):
    farm = farm_detail(farm_id)
    if not farm:
        raise HTTPException(status_code=404, detail="farm not found")
    return templates.TemplateResponse(request, "farm.html", {"user": user, "farm": farm, "csrf": csrf_token(user)})


@app.post("/farms/{farm_id}/harvests")
def add_harvest(farm_id: int, user: str = Depends(current_user), csrf: str = Form(...),
                crop: str = Form(..., pattern=CROP_PATTERN),
                season: str = Form(..., pattern=r"^(19|20)\d\d(-[A-Z][a-z]+)?$"),
                yield_tonnes: float = Form(..., ge=0, le=1_000_000)):
    check_csrf(user, csrf)
    with sessions()() as db:
        if not db.get(Farm, farm_id):
            raise HTTPException(status_code=404, detail="farm not found")
        db.add(Harvest(farm_id=farm_id, crop=crop.strip().lower(), season=season, yield_tonnes=yield_tonnes, recorded_by=user))
        db.commit()
    cache().invalidate(f"farm:{farm_id}", "farms:overview")
    return RedirectResponse(f"/farms/{farm_id}", status_code=303)


# ---------------- JSON API ----------------
class PriceIn(BaseModel):
    crop: str = Field(pattern=CROP_PATTERN)
    market: str = Field(min_length=2, max_length=60)
    price_per_tonne: float = Field(gt=0, le=10_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    on: date


@app.get("/api/prices")
def api_prices(crop: str | None = None, user: str = Depends(current_user)):
    if crop is not None and not re.fullmatch(CROP_PATTERN, crop):
        raise HTTPException(status_code=400, detail="invalid crop")
    return {"prices": latest_prices(crop.lower() if crop else None)}


@app.post("/api/prices", status_code=201)
def publish_price(body: PriceIn, user: str = Depends(current_user)):
    if not is_price_admin(user):
        raise HTTPException(status_code=403, detail="only price administrators can publish prices")
    with sessions()() as db:
        db.add(MarketPrice(crop=body.crop.lower(), market=body.market, price_per_tonne=body.price_per_tonne,
                           currency=body.currency, on=body.on))
        db.commit()
    cache().invalidate("prices:all", f"prices:{body.crop.lower()}")
    return {"published": True}
