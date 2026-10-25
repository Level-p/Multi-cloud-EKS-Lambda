"""Local stand-in for CloudFront + API Gateway + the mobile gateway Lambda, so ZAP can scan the mobile backend in the
pipeline without an AWS account.

    PYTHONPATH=app/mobile-backend python app/dast/mobile_local.py      # http://127.0.0.1:8090

* Every HTTP request goes to the real agripulse_mobile.api.handle(), the code the Lambda handler calls.
* The services it calls (accounts, farm, shop) are a small echo server on 127.0.0.1:8091: the scan tests the
  gateway itself (routing, the allow-list, size limits, the origin check, what it passes on), not the services,
  which have their own DAST scans.
* No origin secret is configured, as if every request came through CloudFront.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Services(BaseHTTPRequestHandler):
    """The upstream services: 401 without a token on protected routes, otherwise an echo of the call."""
    server_version = "services-local"
    sys_version = ""

    def _answer(self):
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        if length:
            self.rfile.read(length)
        public = self.path.startswith(("/api/catalog", "/api/items/", "/api/token", "/api/register"))
        status = 200 if public or self.headers.get("Authorization") else 401
        body = json.dumps({"path": urlsplit(self.path).path} if status == 200 else {"detail": "sign in required"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

    def log_message(self, *args):
        pass


def make_handler(handle):
    class Handler(BaseHTTPRequestHandler):
        server_version = "apigw-local"
        sys_version = ""

        def _handle(self):
            length = min(int(self.headers.get("Content-Length") or 0), 10 * 1024 * 1024)  # CloudFront body limit
            body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            resp = handle(self.command, parts.path, dict(self.headers), body, query=parts.query)
            payload = resp.text().encode()
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            print(json.dumps({"level": "info", "message": fmt % args}), flush=True)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle

    return Handler


def main():
    upstream = ThreadingHTTPServer(("127.0.0.1", 8091), Services)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    for name in ("ACCOUNTS_URL", "FARM_URL", "SHOP_URL"):
        os.environ.setdefault(name, "http://127.0.0.1:8091")
    from agripulse_mobile import api
    port = int(os.environ.get("PORT", "8090"))
    print(json.dumps({"level": "info", "message": f"API Gateway stand-in on http://127.0.0.1:{port}"}), flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(api.handle)).serve_forever()


if __name__ == "__main__":
    main()
