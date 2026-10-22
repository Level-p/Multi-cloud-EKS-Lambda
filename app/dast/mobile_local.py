"""Local stand-in for CloudFront + API Gateway + Lambda + PostgreSQL + S3, so ZAP can scan the mobile backend
in the pipeline without an AWS account.

    PYTHONPATH=app/mobile-backend python app/dast/mobile_local.py      # http://127.0.0.1:8090

* Every HTTP request goes to the real agripulse_mobile.api.handle(), the code the Lambda handler calls.
* SQLite replaces PostgreSQL; moto fakes S3 in memory (the same fakes the unit tests use).
* Any "Authorization: Bearer ..." is accepted as one fixed test farmer, as if API Gateway's Cognito
  authorizer had verified it. Without it, protected routes answer 401 (as the authorizer would).
* No origin secret is configured, as if every request came through CloudFront.
"""
import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

os.environ.update(AWS_DEFAULT_REGION="eu-west-2", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402


def farmer(headers) -> str | None:
    """What the Cognito authorizer would pass on: a fixed farmer for any bearer token."""
    auth = headers.get("Authorization", "")
    return "dast-farmer" if auth.startswith("Bearer ") and len(auth) > 7 else None


def make_handler(handle):
    class Handler(BaseHTTPRequestHandler):
        server_version = "apigw-local"
        sys_version = ""

        def _handle(self):
            length = min(int(self.headers.get("Content-Length") or 0), 10 * 1024 * 1024)  # CloudFront body limit
            body = self.rfile.read(length) if length else b""
            resp = handle(self.command, urlsplit(self.path).path, dict(self.headers), body, user=farmer(self.headers))
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
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="agripulse-media-dast", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        from agripulse_mobile import api
        from agripulse_mobile.db import make_session_factory
        db_url = "sqlite:///" + os.path.join(tempfile.gettempdir(), "dast-mobile.db")
        api.configure(api.Dependencies(sessions=make_session_factory(db_url), s3=s3, media_bucket="agripulse-media-dast",
                                       media_cdn_domain="media.example.com"))
        port = int(os.environ.get("PORT", "8090"))
        print(json.dumps({"level": "info", "message": f"API Gateway stand-in on http://127.0.0.1:{port}"}), flush=True)
        ThreadingHTTPServer(("127.0.0.1", port), make_handler(api.handle)).serve_forever()


if __name__ == "__main__":
    main()
