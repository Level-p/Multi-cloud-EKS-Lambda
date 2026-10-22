from agripulse_mobile import api
from conftest import ORIGIN_SECRET, call

FIELD = {"name": "North plot", "crop": "maize", "area_ha": 12.5, "latitude": 6.45, "longitude": 3.39}


def test_health_needs_no_auth_and_no_origin_header():
    assert call("GET", "/health", user=None, origin=None) == (200, {"status": "ok"})


def test_create_and_list_fields():
    status, field = call("POST", "/fields", FIELD)
    assert status == 201 and field["name"] == "North plot" and field["area_ha"] == 12.5
    assert "owner" not in field   # internal column never returned
    status, body = call("GET", "/fields")
    assert status == 200 and [f["field_id"] for f in body["fields"]] == [field["field_id"]]


def test_fields_are_private_to_their_owner():
    _, field = call("POST", "/fields", FIELD)
    assert call("GET", "/fields", user="farmer-2") == (200, {"fields": []})
    # someone else's field and a field that does not exist look the same
    assert call("GET", f"/fields/{field['field_id']}/observations", user="farmer-2")[0] == 404
    assert call("GET", f"/fields/{'0' * 32}/observations")[0] == 404
    assert call("POST", f"/fields/{field['field_id']}/observations", {"growth_stage": "vegetative", "issue": "none"},
                user="farmer-2")[0] == 404


def test_observations_newest_first():
    _, field = call("POST", "/fields", FIELD)
    path = f"/fields/{field['field_id']}/observations"
    assert call("POST", path, {"growth_stage": "emergence", "issue": "none"})[0] == 201
    assert call("POST", path, {"growth_stage": "vegetative", "issue": "pest", "note": "fall armyworm"})[0] == 201
    status, body = call("GET", path)
    assert status == 200 and [o["growth_stage"] for o in body["observations"]] == ["vegetative", "emergence"]


def test_observation_validation():
    _, field = call("POST", "/fields", FIELD)
    path = f"/fields/{field['field_id']}/observations"
    assert call("POST", path, {"growth_stage": "dancing", "issue": "none"})[0] == 400
    assert call("POST", path, {"growth_stage": "harvest", "issue": "none", "media_keys": ["catalog/x.jpg"]})[0] == 400
    assert call("POST", path, {"growth_stage": "harvest", "issue": "none", "media_keys": ["uploads/farmer-2/a.jpg"]})[0] == 400
    assert call("POST", path, {"growth_stage": "harvest", "issue": "none", "media_keys": ["uploads/../../etc"]})[0] == 400


def test_field_validation_and_bad_json():
    assert call("POST", "/fields", {**FIELD, "area_ha": -1})[0] == 400
    assert call("POST", "/fields", {**FIELD, "crop": "<script>"})[0] == 400
    headers = {"X-Origin-Verify": ORIGIN_SECRET}
    assert api.handle("POST", "/fields", headers, b"{not json", user="farmer-1").status == 400
    assert api.handle("POST", "/fields", headers, b"x" * (api.MAX_BODY_BYTES + 1), user="farmer-1").status == 413


def test_upload_url_is_scoped_to_the_caller():
    status, body = call("POST", "/media/upload-url", {"content_type": "image/jpeg", "size_bytes": 2_000_000})
    assert status == 200 and body["key"].startswith("uploads/farmer-1/") and body["key"].endswith(".jpg")
    conditions = str(body["upload"])
    assert body["upload"]["url"] and body["upload"]["fields"]["Content-Type"] == "image/jpeg"
    assert "uploads/farmer-1/" in conditions
    assert body["cdn_url"] == f"https://media.example.com/{body['key']}"


def test_upload_url_rejects_other_types_and_huge_files():
    assert call("POST", "/media/upload-url", {"content_type": "application/x-msdownload", "size_bytes": 10})[0] == 400
    assert call("POST", "/media/upload-url", {"content_type": "image/png", "size_bytes": 50 * 1024 * 1024})[0] == 400


def test_requests_that_bypass_cloudfront_are_refused():
    assert call("GET", "/fields", origin=None)[0] == 403
    assert call("GET", "/fields", origin="guessed")[0] == 403


def test_unknown_routes_and_methods():
    assert call("GET", "/admin")[0] == 404
    assert call("DELETE", "/fields")[0] == 405
    assert call("POST", "/health", user=None)[0] == 405
