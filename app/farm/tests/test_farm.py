import re

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from agripulse_common.testing import token
from app import main

ADA, BEN = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
PROFILE = {"farm_name": "Ada Farms", "region": "Oyo", "hectares": 12.5, "capacity_tonnes_month": 4, "latitude": 7.38, "longitude": 3.9}
TOMATOES = {"title": "Fresh tomatoes", "kind": "produce", "category": "Vegetables", "unit": "basket", "price": 8500, "quantity": 20}


def bearer(role="farmer", user_id=ADA):
    return {"Authorization": f"Bearer {token(role, user_id=user_id, name='Ada' if user_id == ADA else 'Ben')}"}


INTERNAL = {"X-Internal-Token": "test-internal-token"}


@pytest.fixture
def client():
    main._sessions = None
    return TestClient(main.app)


@pytest.fixture
def listed(client):
    client.put("/api/profile", json=PROFILE, headers=bearer())
    return client.post("/api/listings", json=TOMATOES, headers=bearer()).json()


def test_a_listing_needs_a_farm_profile_first(client):
    assert client.post("/api/listings", json=TOMATOES, headers=bearer()).status_code == 409
    assert client.put("/api/profile", json=PROFILE, headers=bearer()).json()["owner_name"] == "Ada"
    item = client.post("/api/listings", json=TOMATOES, headers=bearer())
    assert item.status_code == 201 and item.json()["currency"] == "NGN"


def test_the_catalog_is_public_and_shows_only_what_is_on_sale(client, listed):
    client.post("/api/listings", json={**TOMATOES, "title": "Seedlings", "kind": "resource", "category": "Nursery"}, headers=bearer())
    client.post("/api/listings", json={**TOMATOES, "title": "Sold out", "quantity": 0}, headers=bearer())
    hidden = client.post("/api/listings", json={**TOMATOES, "title": "Hidden"}, headers=bearer()).json()
    client.patch(f"/api/listings/{hidden['id']}", json={"active": False}, headers=bearer())
    titles = [i["title"] for i in client.get("/api/catalog").json()]
    assert sorted(titles) == ["Fresh tomatoes", "Seedlings"]
    assert [i["title"] for i in client.get("/api/catalog?kind=resource").json()] == ["Seedlings"]
    assert client.get("/api/catalog?q=tomat").json()[0]["farm"]["farm_name"] == "Ada Farms"
    assert client.get("/api/catalog?q=x' OR '1'='1").json() == []   # a bound parameter: just text that matches nothing


def test_farmers_only_touch_their_own_listings_and_buyers_none(client, listed):
    other = client.patch(f"/api/listings/{listed['id']}", json={"price": 1}, headers=bearer(user_id=BEN))
    assert other.status_code == 404
    assert client.get("/api/listings", headers=bearer("buyer", BEN)).status_code == 403
    assert client.get("/api/listings").status_code == 401


def test_reserve_is_atomic_and_never_goes_below_zero(client, listed):
    url = f"/internal/listings/{listed['id']}"
    assert client.post(url + "/reserve", json={"quantity": 15}, headers=INTERNAL).json()["quantity"] == 5
    assert client.post(url + "/reserve", json={"quantity": 6}, headers=INTERNAL).status_code == 409
    client.post(url + "/release", json={"quantity": 15}, headers=INTERNAL)
    assert client.get(f"/api/listings/{listed['id']}").json()["quantity"] == 20


def test_internal_routes_need_the_token_and_refuse_traffic_from_the_load_balancer(client, listed):
    url = f"/internal/listings/{listed['id']}/reserve"
    assert client.post(url, json={"quantity": 1}).status_code == 404
    assert client.post(url, json={"quantity": 1}, headers={"X-Internal-Token": "wrong"}).status_code == 404
    from_alb = {**INTERNAL, "X-Forwarded-For": "203.0.113.9"}
    assert client.post(url, json={"quantity": 1}, headers=from_alb).status_code == 404


def test_activities_are_the_farmers_own_records(client, listed):
    r = client.post("/api/activities", json={"on": "2026-10-20", "kind": "harvest", "crop": "Tomato", "quantity": 40,
                                             "unit": "basket"}, headers=bearer())
    assert r.status_code == 201
    assert len(client.get("/api/activities", headers=bearer()).json()) == 1
    assert client.get("/api/activities", headers=bearer(user_id=BEN)).json() == []


def test_photo_upload_is_a_presigned_post_under_the_farmers_prefix(client, listed):
    with mock_aws():
        boto3.client("s3", region_name="eu-west-2").create_bucket(
            Bucket="agripulse-media-test", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        main._s3 = None
        up = client.post(f"/api/listings/{listed['id']}/photo", headers=bearer()).json()
    assert up["key"].startswith(f"uploads/{ADA}/") and up["image_url"] == f"https://media.test/{up['key']}"
    assert "x-amz-signature" in {k.lower() for k in up["fields"]} or "signature" in {k.lower() for k in up["fields"]}
    assert client.get(f"/api/listings/{listed['id']}").json()["image_url"] == up["image_url"]


def test_pages_send_visitors_to_sign_in_and_forms_are_protected(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("http://accounts.test/login?next=")
    client.cookies.set("agripulse_session", token("farmer", user_id=ADA, name="Ada"))
    page = client.get("/dashboard")
    assert page.status_code == 200 and "Farm profile" in page.text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    assert client.post("/profile", data={"csrf": "forged", "farm_name": "X", "region": "Oyo", "hectares": "1",
                                         "capacity_tonnes_month": "1"}).status_code == 403
    bad = client.post("/profile", data={"csrf": csrf, "farm_name": "Ada Farms", "region": "Oyo", "hectares": "-3",
                                        "capacity_tonnes_month": "1"}, follow_redirects=False)
    assert bad.status_code == 303 and "error=hectares" in bad.headers["location"]
    ok = client.post("/profile", data={"csrf": csrf, "farm_name": "Ada Farms", "region": "Oyo", "hectares": "3",
                                       "capacity_tonnes_month": "1", "latitude": "", "longitude": ""}, follow_redirects=False)
    assert ok.status_code == 303 and "notice=" in ok.headers["location"]
    assert client.get(f"/farms/{ADA}").status_code == 200


def test_health(client):
    assert client.get("/readyz").json() == {"status": "ready"}
