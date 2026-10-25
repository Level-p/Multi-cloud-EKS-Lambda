"""Calls between AgriPulse services, inside the cluster (http://<service>.<service>-<env>.svc:8080).

  user calls      forward the caller's own token: the called service applies its own rules to that user
  internal calls  /internal/... with X-Internal-Token (a per-environment secret shared by the services only).
                  The called service also refuses /internal/ requests that came through the load balancer
                  (they carry X-Forwarded-For), so the token alone is never enough from the internet.
Standard library only (urllib), short timeouts: a slow dependency must not hold a request for long.
"""

import hmac
import json
import os
import urllib.error
import urllib.request

from fastapi import HTTPException, Request

INTERNAL_HEADER = "X-Internal-Token"


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


class ServiceClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, *, body=None, token: str | None = None, internal: bool = False):
        """The decoded JSON answer; ServiceError for any HTTP error or an unreachable service."""
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if internal:
            headers[INTERNAL_HEADER] = os.environ.get("INTERNAL_TOKEN", "")
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            try:
                detail = json.loads(err.read() or b"{}").get("detail", err.reason)
            except ValueError:
                detail = err.reason
            raise ServiceError(err.code, str(detail)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise ServiceError(503, f"service unavailable: {err}") from None


def require_internal(request: Request):
    """FastAPI dependency for /internal/ routes: the shared token, and a request that did not come from outside."""
    expected = os.environ.get("INTERNAL_TOKEN", "")
    given = request.headers.get(INTERNAL_HEADER, "")
    from_outside = "x-forwarded-for" in request.headers or "x-amzn-trace-id" in request.headers
    if not expected or from_outside or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=404, detail="Not Found")   # do not even confirm the route exists
