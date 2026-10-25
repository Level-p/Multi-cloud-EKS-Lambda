"""AgriPulse mobile backend: https://api.<zone> (AWS WAF -> CloudFront -> API Gateway -> this Lambda).

A thin gateway for the mobile app, used by buyers and by farmers. It owns no data: every route is one call to the
production service that owns it, with the user's own token, so the rules (who may see or change what) live in one
place, the services, for the web and the app alike.

  GET  /health
  POST /auth/register   POST /auth/token   GET /me                                       -> accounts
  GET  /catalog         GET /items/{id}                                                   -> shop   (public)
  GET|POST /orders      POST /orders/{id}/cancel   GET /sales   POST /sales/{id}/status   -> shop
  GET|PUT /farm/profile   GET|POST /farm/listings   PATCH|DELETE /farm/listings/{id}
  POST /farm/listings/{id}/photo   GET|POST /farm/activities                              -> farm

Edge: CloudFront adds X-Origin-Verify (a secret only it and this function know); without it every route but
/health answers 403, so the execute-api URL cannot be used to skip the WAF.
The calls to the services go out through CloudFront and the WAF like any client's, with X-Mobile-Gateway (a
second secret) so the per-IP rate limit does not treat all app users, who share this function's addresses, as one.
Standard library only: nothing to patch, a tiny package.
"""

import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("agripulse.mobile")

MAX_BODY_BYTES = 64 * 1024
MAX_QUERY = 500
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "8"))
HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
           "Strict-Transport-Security": "max-age=31536000"}

# method(s), path, service, path at the service ({0} = the id from the path)
ROUTES = [
    ("POST", r"/auth/register", "accounts", "/api/register"),
    ("POST", r"/auth/token", "accounts", "/api/token"),
    ("GET", r"/me", "accounts", "/api/me"),
    ("GET", r"/catalog", "shop", "/api/catalog"),
    ("GET", r"/items/(\d{1,12})", "shop", "/api/items/{0}"),
    ("GET|POST", r"/orders", "shop", "/api/orders"),
    ("POST", r"/orders/(\d{1,12})/cancel", "shop", "/api/orders/{0}/cancel"),
    ("GET", r"/sales", "shop", "/api/sales"),
    ("POST", r"/sales/(\d{1,12})/status", "shop", "/api/sales/{0}/status"),
    ("GET|PUT", r"/farm/profile", "farm", "/api/profile"),
    ("GET|POST", r"/farm/listings", "farm", "/api/listings"),
    ("PATCH|DELETE", r"/farm/listings/(\d{1,12})", "farm", "/api/listings/{0}"),
    ("POST", r"/farm/listings/(\d{1,12})/photo", "farm", "/api/listings/{0}/photo"),
    ("GET|POST", r"/farm/activities", "farm", "/api/activities"),
]
COMPILED = [(set(m.split("|")), re.compile(f"^{p}/?$"), svc, target) for m, p, svc, target in ROUTES]
SERVICE_URL_ENV = {"accounts": "ACCOUNTS_URL", "farm": "FARM_URL", "shop": "SHOP_URL"}


@dataclass
class Response:
    status: int
    body: object = None
    headers: dict = field(default_factory=lambda: dict(HEADERS))

    def text(self) -> str:
        return json.dumps(self.body) if self.body is not None else ""


def error(status: int, message: str) -> Response:
    return Response(status, {"detail": message})


def origin_ok(headers: dict) -> bool:
    expected = os.environ.get("ORIGIN_VERIFY_SECRET", "")
    if not expected:   # the DAST stand-in and the tests run without CloudFront
        return True
    given = next((v for k, v in headers.items() if k.lower() == "x-origin-verify"), "")
    return hmac.compare_digest(given.encode(), expected.encode())


def header(headers: dict, name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name), None)


def route(method: str, path: str):
    for methods, pattern, service, target in COMPILED:
        m = pattern.match(path)
        if m:
            if method not in methods:
                return "method", None
            return service, target.format(*m.groups())
    return None, None


def forward(service: str, method: str, target: str, query: str, headers: dict, body: bytes) -> Response:
    base = os.environ.get(SERVICE_URL_ENV[service], "").rstrip("/")
    if not base:
        return error(503, f"{service} is not configured")
    url = base + target + (f"?{query}" if query else "")
    out = {"Accept": "application/json", "User-Agent": "agripulse-mobile-gateway/1",
           "X-Mobile-Gateway": os.environ.get("MOBILE_GATEWAY_SECRET", "")}
    for name in ("authorization", "content-type"):
        value = header(headers, name)
        if value:
            out[name.title()] = value
    req = urllib.request.Request(url, data=body if method in ("POST", "PUT", "PATCH") else None, method=method, headers=out)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return Response(resp.status, json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            payload = json.loads(raw) if raw else {"detail": err.reason}
        except ValueError:
            payload = {"detail": err.reason}
        resp = Response(err.code, payload)
        if err.code == 401:
            resp.headers["WWW-Authenticate"] = "Bearer"
        return resp
    except (urllib.error.URLError, TimeoutError, OSError):
        log.error(json.dumps({"event": "upstream unavailable", "service": service}))
        return error(503, f"{service} is unavailable, try again shortly")


def handle(method: str, path: str, headers: dict, body: bytes, query: str = "") -> Response:
    started = time.perf_counter()
    method = method.upper()
    if path == "/health" and method == "GET":
        return Response(200, {"status": "ok"})
    if not origin_ok(headers):
        return error(403, "forbidden")
    if len(body) > MAX_BODY_BYTES:
        return error(413, "request body too large")
    if len(query) > MAX_QUERY:
        return error(414, "query string too long")
    service, target = route(method, path)
    if service is None:
        return error(404, "not found")
    if service == "method":
        return error(405, "method not allowed")
    resp = forward(service, method, target, query, headers, body)
    log.info(json.dumps({"route": path if service != "farm" else re.sub(r"\d+", "{id}", path), "method": method,
                         "service": service, "status": resp.status, "ms": round((time.perf_counter() - started) * 1000)}))
    return resp


def query_string(event: dict) -> str:
    multi = event.get("multiValueQueryStringParameters") or {}
    if multi:
        return urllib.parse.urlencode([(k, v) for k, vs in multi.items() for v in vs])
    return urllib.parse.urlencode(event.get("queryStringParameters") or {})
