import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))   # app/mobile-backend: agripulse_mobile

ORIGIN_SECRET = "origin-secret-for-tests"


class Upstream(BaseHTTPRequestHandler):
    """Stands in for accounts, farm and shop: answers with what it received, or a status chosen by the path."""
    seen: list = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        record = {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                  "body": body.decode()}
        Upstream.seen.append(record)
        status = 401 if "/api/me" in self.path and "authorization" not in record["headers"] else 200
        payload = json.dumps({"detail": "sign in required"} if status == 401 else {"echo": record["path"]}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    yield base
    server.shutdown()


@pytest.fixture(autouse=True)
def env(upstream, monkeypatch):
    Upstream.seen.clear()
    for name in ("ACCOUNTS_URL", "FARM_URL", "SHOP_URL"):
        monkeypatch.setenv(name, upstream)
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", ORIGIN_SECRET)
    monkeypatch.setenv("MOBILE_GATEWAY_SECRET", "gateway-secret")
    yield Upstream.seen
