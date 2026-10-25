"""The FastAPI app every AgriPulse service starts from.

  create_app()   docs off (no /docs, no /openapi.json in production), OpenTelemetry, request metrics,
                 security headers, /healthz (process up), /readyz (dependencies up), /metrics (Prometheus)
  csrf           every form carries a token derived from a random per-browser cookie (HMAC with the
                 service's CSRF_SECRET): another site can neither read the cookie nor guess the token
  LoginRequired  HTML pages send visitors without a session to the accounts sign-in page

Edge protection (bots, SQL injection, rate limits) is AWS WAF on the ALB; the services still validate every
input with Pydantic and only use bound SQL parameters.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from . import telemetry
from .auth import LoginRequired

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request duration", ["method", "route"],
                    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
CSRF_COOKIE = "agripulse_csrf"


def configure_logging():
    logging.basicConfig(level=logging.INFO,
                        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}')


def cookie_secure() -> bool:
    # Off only where the app runs over plain HTTP on purpose (local runs, the DAST scan on the CI runner)
    return os.environ.get("COOKIE_SECURE", "true").lower() != "false"


def security_headers() -> dict[str, str]:
    media = os.environ.get("MEDIA_CDN_DOMAIN", "")
    img = "'self'" + (f" https://{media}" if media else "")
    return {
        "Content-Security-Policy": f"default-src 'self'; style-src 'self'; script-src 'none'; img-src {img}; "
                                   "frame-ancestors 'none'; form-action 'self' https:; base-uri 'none'",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin",
        "Strict-Transport-Security": "max-age=31536000", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }


SHARED = Path(__file__).parent   # base.html and style.css, the same in every service


def create_app(title: str, base_dir: Path, *, readiness=None, on_metrics=None, loggers=(), redis=False) -> tuple[FastAPI, Jinja2Templates]:
    """The app and its templates: base_dir/templates first, then the shared ones (base.html).
    on_metrics runs before each Prometheus scrape (gauges read from the database)."""
    configure_logging()
    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
    telemetry.setup(app, loggers=loggers, redis=redis)   # off without OTEL_EXPORTER_OTLP_ENDPOINT
    templates = Jinja2Templates(directory=[str(base_dir / "templates"), str(SHARED / "templates")])   # autoescape on
    templates.env.globals["csrf_token"] = csrf_token
    templates.env.globals["env"] = {
        "environment": os.environ.get("ENVIRONMENT", "local"),
        "accounts_url": os.environ.get("ACCOUNTS_URL", ""), "farm_url": os.environ.get("FARM_URL", ""),
        "shop_url": os.environ.get("SHOP_URL", ""), "media": os.environ.get("MEDIA_CDN_DOMAIN", ""),
    }
    app.mount("/static", StaticFiles(directory=str(SHARED / "static")), name="static")
    headers = security_headers()

    @app.middleware("http")
    async def observe(request: Request, call_next):
        start = time.perf_counter()
        if not request.cookies.get(CSRF_COOKIE):
            request.state.csrf_seed = secrets.token_urlsafe(32)
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        LATENCY.labels(request.method, route).observe(time.perf_counter() - start)
        for k, v in headers.items():
            response.headers.setdefault(k, v)
        seed = getattr(request.state, "csrf_seed", None)
        if seed:
            response.set_cookie(CSRF_COOKIE, seed, httponly=True, secure=cookie_secure(), samesite="strict", path="/")
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        return RedirectResponse(exc.login_url, status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            if readiness:
                readiness()
            return {"status": "ready"}
        except Exception:
            logging.getLogger(title).exception("not ready")
            raise HTTPException(status_code=503, detail="dependency unavailable")

    @app.get("/metrics")
    def metrics():
        if on_metrics:
            try:
                on_metrics()
            except Exception:
                logging.getLogger(title).exception("could not refresh the gauges")
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app, templates


# ---------------- CSRF ----------------
def _seed(request: Request) -> str:
    return getattr(request.state, "csrf_seed", None) or request.cookies.get(CSRF_COOKIE, "")


def csrf_token(request: Request) -> str:
    secret = os.environ.get("CSRF_SECRET", "")
    return hmac.new(secret.encode(), _seed(request).encode(), hashlib.sha256).hexdigest() if secret else ""


def check_csrf(request: Request, token: str):
    expected = csrf_token(request)
    if not expected or not request.cookies.get(CSRF_COOKIE) or not hmac.compare_digest(token or "", expected):
        raise HTTPException(status_code=403, detail="invalid form token, reload the page")


def page(templates: Jinja2Templates, request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"request": request, **context}, status_code=status_code)
