import re

import jwt
import pytest
from fastapi.testclient import TestClient

from agripulse_common import auth
from app import admin, main

PASSWORD = "correct horse battery"


@pytest.fixture
def client():
    main._sessions = None   # a fresh SQLite database per test
    return TestClient(main.app)


def form_csrf(client, path="/register"):
    page = client.get(path)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)


def test_register_on_the_web_signs_in_and_goes_to_the_role_home(client):
    csrf = form_csrf(client)
    r = client.post("/register", data={"csrf": csrf, "name": "Ada Farmer", "email": "Ada@Example.com", "password": PASSWORD,
                                       "role": "farmer"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "http://farm.test/dashboard"
    me = auth.verify_token(r.cookies[auth.cookie_name()], auth.public_key())
    assert (me.email, me.role, me.name) == ("ada@example.com", "farmer", "Ada Farmer")


def test_forms_need_the_csrf_token(client):
    r = client.post("/register", data={"csrf": "forged", "name": "X Y", "email": "x@example.com", "password": PASSWORD,
                                       "role": "buyer"})
    assert r.status_code == 403


def test_ops_cannot_be_chosen_at_sign_up(client):
    r = client.post("/api/register", json={"email": "o@example.com", "password": PASSWORD, "name": "Op", "role": "ops"})
    assert r.status_code == 422


def test_api_register_then_token_then_me(client):
    r = client.post("/api/register", json={"email": "b@example.com", "password": PASSWORD, "name": "Bola", "role": "buyer"})
    assert r.status_code == 201 and r.json()["user"]["role"] == "buyer"
    assert client.post("/api/register", json={"email": "B@example.com", "password": PASSWORD, "name": "Bola",
                                              "role": "buyer"}).status_code == 409
    tok = client.post("/api/token", json={"email": "b@example.com", "password": PASSWORD}).json()["token"]
    assert client.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).json()["email"] == "b@example.com"
    assert client.get("/api/me").status_code == 401


def test_wrong_password_and_unknown_email_look_the_same_and_five_failures_lock(client):
    client.post("/api/register", json={"email": "c@example.com", "password": PASSWORD, "name": "Chi", "role": "buyer"})
    unknown = client.post("/api/token", json={"email": "nobody@example.com", "password": PASSWORD})
    wrong = client.post("/api/token", json={"email": "c@example.com", "password": "wrong password!"})
    assert unknown.status_code == wrong.status_code == 401 and unknown.json() == wrong.json()
    for _ in range(4):
        client.post("/api/token", json={"email": "c@example.com", "password": "wrong password!"})
    assert client.post("/api/token", json={"email": "c@example.com", "password": PASSWORD}).status_code == 429


def test_next_only_goes_back_to_agripulse(monkeypatch):
    monkeypatch.setenv("COOKIE_DOMAIN", "mfon21.space")
    assert main.safe_next("https://shop.mfon21.space/cart", "/d") == "https://shop.mfon21.space/cart"
    assert main.safe_next("https://evil.example/mfon21.space", "/d") == "/d"
    assert main.safe_next("https://mfon21.space.evil.example/", "/d") == "/d"
    assert main.safe_next("//evil.example", "/d") == "/d"
    assert main.safe_next("http://shop.mfon21.space/", "/d") == "/d"


def test_grant_ops_is_an_operator_command(client):
    client.post("/api/register", json={"email": "ops@example.com", "password": PASSWORD, "name": "Ops", "role": "buyer"})
    assert "now ops" in admin.set_role("ops@example.com", "ops")
    tok = client.post("/api/token", json={"email": "ops@example.com", "password": PASSWORD}).json()["token"]
    assert auth.verify_token(tok, auth.public_key()).role == "ops"


def test_tokens_from_another_key_or_issuer_are_refused(monkeypatch):
    from agripulse_common.testing import key_pair
    other_private, _ = key_pair()
    forged = auth.issue_token(other_private, user_id="u1", email="x@example.com", name="X", role="ops")
    with pytest.raises(jwt.PyJWTError):
        auth.verify_token(forged, auth.public_key())
    good = auth.issue_token(main.private_key(), user_id="u1", email="x@example.com", name="X", role="buyer")
    monkeypatch.setenv("JWT_ISSUER", "agripulse-accounts-production")
    with pytest.raises(jwt.PyJWTError):
        auth.verify_token(good, auth.public_key())


def test_health_and_metrics(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
    assert "agripulse_logins_total" in client.get("/metrics").text
    assert "frame-ancestors 'none'" in client.get("/login").headers["content-security-policy"]
