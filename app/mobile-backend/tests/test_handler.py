import base64
import json

from agripulse_mobile import api, handler

TOKEN = "Bearer header.payload.signature"


def event(method, path, body=None, auth=TOKEN, query=None, b64=False):
    raw = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json", **({"Authorization": auth} if auth else {})}
    return {"resource": "/{proxy+}", "path": path, "httpMethod": method, "headers": headers,
            "multiValueQueryStringParameters": query, "requestContext": {"requestId": "r-1", "stage": "prod"},
            "body": base64.b64encode(raw.encode()).decode() if (raw and b64) else raw, "isBase64Encoded": b64}


def call(*args, **kwargs):
    resp = handler.handler(event(*args, **kwargs), None)
    return resp["statusCode"], json.loads(resp["body"]) if resp["body"] else None


def test_each_route_goes_to_its_service_with_the_users_token(env):
    cases = [("POST", "/auth/token", "/api/token"), ("GET", "/catalog", "/api/catalog"), ("GET", "/items/7", "/api/items/7"),
             ("POST", "/orders", "/api/orders"), ("POST", "/orders/3/cancel", "/api/orders/3/cancel"),
             ("POST", "/sales/3/status", "/api/sales/3/status"), ("PUT", "/farm/profile", "/api/profile"),
             ("PATCH", "/farm/listings/9", "/api/listings/9"), ("POST", "/farm/listings/9/photo", "/api/listings/9/photo"),
             ("GET", "/farm/activities", "/api/activities")]
    for method, path, target in cases:
        status, body = call(method, path, {"x": 1} if method in ("POST", "PUT", "PATCH") else None)
        assert status == 200 and body == {"echo": target}, (method, path)
    for seen in env:
        assert seen["headers"]["authorization"] == TOKEN and seen["headers"]["x-mobile-gateway"] == "gateway-secret"
    assert json.loads(env[0]["body"]) == {"x": 1}


def test_query_strings_and_base64_bodies_are_passed_on(env):
    status, body = call("GET", "/catalog", query={"q": ["yam"], "kind": ["produce"]})
    assert body == {"echo": "/api/catalog?q=yam&kind=produce"}
    call("POST", "/orders", {"items": []}, b64=True)
    assert json.loads(env[-1]["body"]) == {"items": []}


def test_the_services_answer_is_returned_as_is(env):
    status, body = call("GET", "/me", auth=None)
    assert status == 401 and body == {"detail": "sign in required"}


def test_health_answers_without_calling_a_service(env):
    assert call("GET", "/health", auth=None) == (200, {"status": "ok"})
    assert env == []


def test_unknown_routes_methods_and_oversized_requests(env):
    assert call("GET", "/internal/listings/1/reserve")[0] == 404   # never a way to the internal API
    assert call("DELETE", "/catalog")[0] == 405
    assert call("GET", "/items/../../internal")[0] == 404
    assert api.handle("POST", "/orders", {}, b"x" * (api.MAX_BODY_BYTES + 1)).status == 413
    assert env == []


def test_a_service_that_is_down_is_a_503(monkeypatch):
    monkeypatch.setenv("SHOP_URL", "http://127.0.0.1:9")   # nothing listens there
    status, body = call("GET", "/catalog")
    assert status == 503 and "unavailable" in body["detail"]
