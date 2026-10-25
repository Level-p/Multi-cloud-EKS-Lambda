import re

import fakeredis
import pytest
from fastapi.testclient import TestClient

from agripulse_common.cache import Cache
from agripulse_common.services import ServiceError
from agripulse_common.testing import token
from app import main

ADA, OBI, BOLA = "11111111-1111-4111-8111-111111111111", "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"


class FakeFarm:
    """The farm service's public and internal API, in memory, with the same rules (stock never below zero)."""

    def __init__(self):
        self.listings = {
            1: {"id": 1, "owner_id": ADA, "title": "Tomatoes", "kind": "produce", "category": "Vegetables", "unit": "basket",
                "price": 8500.0, "currency": "NGN", "quantity": 10.0, "image_url": None, "active": True,
                "farm": {"owner_id": ADA, "farm_name": "Ada Farms", "region": "Oyo"}},
            2: {"id": 2, "owner_id": OBI, "title": "Seedlings", "kind": "resource", "category": "Nursery", "unit": "tray",
                "price": 1200.0, "currency": "NGN", "quantity": 5.0, "image_url": None, "active": True,
                "farm": {"owner_id": OBI, "farm_name": "Obi Nursery", "region": "Enugu"}},
        }
        self.calls = []

    def call(self, method, path, body=None, token=None, internal=False):
        self.calls.append((method, path))
        if path.startswith("/api/catalog"):
            return [dict(v) for v in self.listings.values() if v["quantity"] > 0]
        m = re.match(r"^/api/listings/(\d+)$", path)
        if m:
            if int(m.group(1)) not in self.listings:
                raise ServiceError(404, "listing not found")
            return dict(self.listings[int(m.group(1))])
        m = re.match(r"^/internal/listings/(\d+)/(reserve|release)$", path)
        assert m and internal, path
        item = self.listings.get(int(m.group(1)))
        if item is None:
            raise ServiceError(404, "listing not found")
        if m.group(2) == "reserve":
            if item["quantity"] < body["quantity"]:
                raise ServiceError(409, "not enough in stock")
            item["quantity"] -= body["quantity"]
            return dict(item)
        item["quantity"] += body["quantity"]
        return {"released": body["quantity"]}


@pytest.fixture
def farm(monkeypatch):
    fake = FakeFarm()
    main._sessions = None
    main._farm = fake
    main._cache = Cache(fakeredis.FakeRedis(decode_responses=True))
    return fake


@pytest.fixture
def client(farm):
    return TestClient(main.app)


def bearer(role, user_id, name="Bola"):
    return {"Authorization": f"Bearer {token(role, user_id=user_id, name=name)}"}


ADDRESS = {"delivery_address": "12 Allen Avenue, Ikeja", "phone": "+234 801 234 5678"}


def test_catalog_is_public_and_cached(client, farm):
    assert [i["title"] for i in client.get("/api/catalog").json()] == ["Tomatoes", "Seedlings"]
    client.get("/api/catalog")
    assert sum(1 for c in farm.calls if c[1].startswith("/api/catalog")) == 1   # the second answer came from Redis
    assert "Tomatoes" in client.get("/").text


def test_an_order_per_farm_with_prices_frozen_and_stock_taken(client, farm):
    r = client.post("/api/orders", json={"items": [{"listing_id": 1, "quantity": 2}, {"listing_id": 2, "quantity": 3}], **ADDRESS},
                    headers=bearer("buyer", BOLA))
    assert r.status_code == 201
    orders = {o["farm"]["farm_name"]: o for o in r.json()}
    assert set(orders) == {"Ada Farms", "Obi Nursery"}
    assert orders["Ada Farms"]["total"] == 17000.0 and orders["Ada Farms"]["payment"] == "on-delivery"
    assert farm.listings[1]["quantity"] == 8 and farm.listings[2]["quantity"] == 2
    farm.listings[1]["price"] = 1.0
    assert client.get("/api/orders", headers=bearer("buyer", BOLA)).json()[-1]["total"] == 17000.0


def test_if_one_item_cannot_be_reserved_nothing_is_ordered(client, farm):
    r = client.post("/api/orders", json={"items": [{"listing_id": 1, "quantity": 2}, {"listing_id": 2, "quantity": 99}], **ADDRESS},
                    headers=bearer("buyer", BOLA))
    assert r.status_code == 409
    assert farm.listings[1]["quantity"] == 10   # the tomatoes reserved first were released
    assert client.get("/api/orders", headers=bearer("buyer", BOLA)).json() == []


def test_farmers_can_buy_resources_but_not_their_own_listing(client, farm):
    ok = client.post("/api/orders", json={"items": [{"listing_id": 2, "quantity": 1}], **ADDRESS}, headers=bearer("farmer", ADA, "Ada"))
    assert ok.status_code == 201
    own = client.post("/api/orders", json={"items": [{"listing_id": 1, "quantity": 1}], **ADDRESS}, headers=bearer("farmer", ADA, "Ada"))
    assert own.status_code == 409 and farm.listings[1]["quantity"] == 10
    assert client.post("/api/orders", json={"items": [{"listing_id": 1, "quantity": 1}], **ADDRESS},
                       headers=bearer("ops", BOLA)).status_code == 403


def test_the_farmer_moves_the_order_and_a_cancellation_returns_the_stock(client, farm):
    order = client.post("/api/orders", json={"items": [{"listing_id": 1, "quantity": 4}], **ADDRESS},
                        headers=bearer("buyer", BOLA)).json()[0]
    ada = bearer("farmer", ADA, "Ada")
    assert [o["id"] for o in client.get("/api/sales", headers=ada).json()] == [order["id"]]
    assert client.get("/api/sales", headers=bearer("farmer", OBI, "Obi")).json() == []
    assert client.post(f"/api/sales/{order['id']}/status", json={"status": "delivered"}, headers=ada).status_code == 409
    assert client.post(f"/api/sales/{order['id']}/status", json={"status": "accepted"}, headers=ada).json()["status"] == "accepted"
    assert client.post(f"/api/orders/{order['id']}/cancel", headers=bearer("buyer", BOLA)).status_code == 409   # too late for the buyer
    assert client.post(f"/api/sales/{order['id']}/status", json={"status": "cancelled"}, headers=ada).json()["status"] == "cancelled"
    assert farm.listings[1]["quantity"] == 10


def test_cart_checkout_on_the_web(client, farm):
    client.cookies.set("agripulse_session", token("buyer", user_id=BOLA, name="Bola"))
    page = client.get("/items/1")
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    assert client.post("/cart", data={"csrf": csrf, "listing_id": "1", "quantity": "3"}, follow_redirects=False).status_code == 303
    assert "25500.00 NGN" in client.get("/cart").text
    done = client.post("/checkout", data={"csrf": csrf, **ADDRESS}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/orders?placed=1"
    assert "Ada Farms" in client.get("/orders").text and farm.listings[1]["quantity"] == 7
    assert "is empty" in client.get("/cart").text


def test_pages_need_sign_in_and_the_right_role(client):
    assert client.get("/cart", follow_redirects=False).headers["location"].startswith("http://accounts.test/login?next=")
    client.cookies.set("agripulse_session", token("buyer", user_id=BOLA))
    assert client.get("/sales").status_code == 403


def test_farm_service_down_is_a_clear_503_not_a_crash(client, farm):
    def down(*a, **kw):
        raise ServiceError(503, "service unavailable")
    farm.call = down
    main._cache = Cache(None)
    assert client.get("/api/catalog").status_code == 503
    assert "unavailable" in client.get("/").text
