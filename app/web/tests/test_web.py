from datetime import date

import fakeredis
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import app.main as web
from app.cache import Cache
from app.db import make_session_factory

USER = {"X-Auth-Request-Email": "Agronomist@Coop.example"}
ADMIN = {"X-Auth-Request-Email": "prices@coop.example"}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("CSRF_SECRET", "test-secret")
    monkeypatch.setenv("PRICE_ADMINS", "prices@coop.example")
    monkeypatch.delenv("DEV_USER", raising=False)
    web._sessions = make_session_factory("sqlite://")
    web._cache = Cache(fakeredis.FakeRedis(decode_responses=True))
    yield
    web._sessions, web._cache = None, None


@pytest.fixture
def client():
    return TestClient(web.app)


def counter(name, label):
    return REGISTRY.get_sample_value(name, {"key": label}) or 0


def csrf(user="agronomist@coop.example"):
    return web.csrf_token(user)


def add_farm(client, name="Green Valley", hectares="20"):
    r = client.post("/farms", data={"csrf": csrf(), "name": name, "region": "Oyo", "hectares": hectares},
                    headers=USER, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_requires_sign_in(client):
    assert client.get("/").status_code == 401
    assert client.get("/api/prices").status_code == 401


def test_health_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
    assert b"agripulse_cache_hits_total" in client.get("/metrics").content


def test_dashboard_has_strict_csp_and_escapes(client):
    add_farm(client, name="<script>alert(1)</script>")
    r = client.get("/", headers=USER)
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text
    assert "script-src 'none'" in r.headers["content-security-policy"]


def test_csrf_is_required(client):
    r = client.post("/farms", data={"csrf": "wrong", "name": "X farm", "region": "Oyo", "hectares": "2"}, headers=USER)
    assert r.status_code == 403


def test_harvest_updates_farm_and_overview(client):
    fid = add_farm(client)
    r = client.post(f"/farms/{fid}/harvests", data={"csrf": csrf(), "crop": "Maize", "season": "2026-Rainy",
                                                    "yield_tonnes": "42.5"}, headers=USER, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/farms/{fid}", headers=USER).text
    assert "42.5" in page and "2.12" in page   # tonnes per hectare
    assert "42.5" in client.get("/", headers=USER).text


def test_cache_aside_hit_then_invalidation(client):
    fid = add_farm(client)
    before_miss, before_hit = counter("agripulse_cache_misses_total", "farm"), counter("agripulse_cache_hits_total", "farm")
    client.get(f"/farms/{fid}", headers=USER)          # miss -> SQL -> cached
    client.get(f"/farms/{fid}", headers=USER)          # hit
    assert counter("agripulse_cache_misses_total", "farm") == before_miss + 1
    assert counter("agripulse_cache_hits_total", "farm") == before_hit + 1
    client.post(f"/farms/{fid}/harvests", data={"csrf": csrf(), "crop": "cassava", "season": "2026", "yield_tonnes": "7"},
                headers=USER)
    assert "cassava" in client.get(f"/farms/{fid}", headers=USER).text   # stale entry was deleted


def test_redis_down_falls_back_to_sql(client):
    class Broken:
        def get(self, *_): raise ConnectionError("redis down")
        def set(self, *_, **__): raise ConnectionError("redis down")
        def delete(self, *_): raise ConnectionError("redis down")
    web._cache = Cache(Broken())
    fid = add_farm(client)
    assert client.get(f"/farms/{fid}", headers=USER).status_code == 200


def test_prices_api_admin_only_and_cached(client):
    body = {"crop": "Maize", "market": "Bodija", "price_per_tonne": 410000, "currency": "NGN", "on": str(date(2026, 10, 1))}
    assert client.post("/api/prices", json=body, headers=USER).status_code == 403
    assert client.post("/api/prices", json=body, headers=ADMIN).status_code == 201
    prices = client.get("/api/prices?crop=maize", headers=USER).json()["prices"]
    assert prices[0]["market"] == "Bodija"
    body2 = body | {"market": "Mile 12", "on": str(date(2026, 10, 2))}
    client.post("/api/prices", json=body2, headers=ADMIN)
    assert client.get("/api/prices?crop=maize", headers=USER).json()["prices"][0]["market"] == "Mile 12"


@pytest.mark.parametrize("crop", ["maize' OR 1=1--", "<b>", "x" * 60])
def test_sql_injection_like_input_is_rejected(client, crop):
    assert client.get("/api/prices", params={"crop": crop}, headers=USER).status_code == 400


@pytest.mark.parametrize("data", [
    {"name": "A", "region": "Oyo", "hectares": "2"},            # name too short
    {"name": "Good farm", "region": "Oyo;--", "hectares": "2"},  # region pattern
    {"name": "Good farm", "region": "Oyo", "hectares": "-5"},    # hectares > 0
])
def test_farm_form_validation(client, data):
    assert client.post("/farms", data={"csrf": csrf(), **data}, headers=USER).status_code == 422


def test_unknown_farm_is_404(client):
    assert client.get("/farms/999", headers=USER).status_code == 404


def test_redis_client_targets_the_in_cluster_valkey(monkeypatch):
    from app.cache import make_client

    monkeypatch.delenv("REDIS_HOST", raising=False)
    assert make_client() is None                                   # no cache configured: SQL only
    monkeypatch.setenv("REDIS_HOST", "agripulse-redis")
    monkeypatch.setenv("REDIS_PASSWORD", "pw")
    monkeypatch.delenv("REDIS_PORT", raising=False)
    monkeypatch.delenv("REDIS_TLS", raising=False)
    kw = make_client().connection_pool.connection_kwargs
    assert (kw["host"], kw["port"], kw["password"]) == ("agripulse-redis", 6379, "pw")
    assert "ssl" not in str(make_client().connection_pool.connection_class).lower()   # plain TCP in the cluster
    monkeypatch.setenv("REDIS_TLS", "true")
    assert "ssl" in str(make_client().connection_pool.connection_class).lower()       # opt-in TLS
