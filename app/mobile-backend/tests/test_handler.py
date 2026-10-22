import base64
import json

from agripulse_mobile import handler
from agripulse_mobile.db import iam_token_provider
from conftest import ORIGIN_SECRET


def event(method, path, body=None, sub="farmer-1", origin=ORIGIN_SECRET, b64=False):
    raw = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json", **({"X-Origin-Verify": origin} if origin else {})}
    return {"resource": "/{proxy+}", "path": path, "httpMethod": method, "headers": headers,
            "requestContext": {"requestId": "r-1", "stage": "prod", "authorizer": {"claims": {"sub": sub}} if sub else None},
            "body": base64.b64encode(raw.encode()).decode() if (raw and b64) else raw, "isBase64Encoded": b64}


def test_authorizer_claims_identify_the_farmer():
    created = handler.handler(event("POST", "/fields", {"name": "Plot", "crop": "maize", "area_ha": 2,
                                                        "latitude": 6.4, "longitude": 3.4}), None)
    assert created["statusCode"] == 201 and created["headers"]["Cache-Control"] == "no-store"
    mine = json.loads(handler.handler(event("GET", "/fields"), None)["body"])["fields"]
    theirs = json.loads(handler.handler(event("GET", "/fields", sub="farmer-2"), None)["body"])["fields"]
    assert len(mine) == 1 and theirs == []


def test_no_claims_means_401_and_health_needs_none():
    assert handler.handler(event("GET", "/fields", sub=None), None)["statusCode"] == 401
    assert handler.handler(event("GET", "/health", sub=None, origin=None), None)["statusCode"] == 200


def test_base64_bodies_are_decoded():
    resp = handler.handler(event("POST", "/media/upload-url", {"content_type": "image/png", "size_bytes": 100}, b64=True), None)
    assert resp["statusCode"] == 200 and json.loads(resp["body"])["key"].endswith(".png")


def test_every_new_connection_gets_a_fresh_iam_token():
    calls = []

    class Rds:
        def generate_db_auth_token(self, **kw):
            calls.append(kw)
            return f"token-{len(calls)}"

    hook = iam_token_provider(Rds(), "db.example", 5432, "agripulse_mobile", "eu-west-2")
    first, second = {}, {}
    hook(None, None, [], first)
    hook(None, None, [], second)
    assert (first["password"], second["password"]) == ("token-1", "token-2")
    assert calls[0] == {"DBHostname": "db.example", "Port": 5432, "DBUsername": "agripulse_mobile", "Region": "eu-west-2"}
