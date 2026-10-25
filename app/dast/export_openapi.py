"""Write the OpenAPI document of an AgriPulse service from its own code, and seed records so the scan reaches real data.

    python app/dast/export_openapi.py farm app/dast/openapi/farm.json --seed http://127.0.0.1:8080

The services serve no /openapi.json in production (openapi_url=None: less to discover for an attacker), but FastAPI
can still generate the document from the routes and Pydantic models. ZAP imports it, so the active scan reaches
every operation and form field. Because it is generated, it can never drift from the code.

Integer path ids ({listing_id}, {order_id}, {incident_id}) get the example 1: --seed creates that first record through
the service's own API with the scan's token (DAST_TOKEN, a real signed token: app/dast/start-service.sh), so
/api/listings/1 and /incidents/1 exist.
"""
import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = {"farm_name": "DAST farm", "region": "Oyo", "hectares": 12.5, "capacity_tonnes_month": 4}
LISTING = {"title": "DAST tomatoes", "kind": "produce", "category": "Vegetables", "unit": "basket", "price": 8500,
           "quantity": 1000}


def call(base: str, method: str, path: str, body=None, token=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(base + path, data=data, method=method, headers=hdrs), timeout=10) as r:
        print(f"seeded {method} {path}: {r.status}")


def seed_farm(base: str, token: str):
    call(base, "PUT", "/api/profile", PROFILE, token)
    call(base, "POST", "/api/listings", LISTING, token)


def seed(service: str, base: str):
    token = os.environ["DAST_TOKEN"]
    if service == "accounts":
        call(base, "POST", "/api/register", {"email": "dast@example.com", "password": "dast-password-1", "name": "DAST buyer",
                                             "role": "buyer"})
    elif service == "farm":
        seed_farm(base, token)
    elif service == "shop":
        # the farm service the shop calls (started next to it) needs a listing from another farmer
        seed_farm(os.environ["FARM_INTERNAL_URL"], os.environ["DAST_FARMER_TOKEN"])
        call(base, "POST", "/api/orders", {"items": [{"listing_id": 1, "quantity": 1}], "delivery_address": "1 DAST Road, Ibadan",
                                           "phone": "+234 800 000 0000"}, token)
    elif service == "incidents":
        call(base, "POST", "/webhooks/alertmanager", {"status": "firing", "groupKey": "dast", "commonLabels": {
            "alertname": "DastProbe", "severity": "warning", "namespace": "dast"}, "alerts": [{"annotations": {"summary": "seeded"}}]},
             headers={"Authorization": f"Bearer {os.environ['WEBHOOK_TOKEN']}"})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("service", choices=["accounts", "farm", "shop", "incidents"])
    p.add_argument("out")
    p.add_argument("--seed", metavar="BASE_URL")
    args = p.parse_args()

    sys.path[:0] = [os.path.join(HERE, "..", args.service), os.path.join(HERE, "..", "common")]   # app/<service>, app/common
    from app.main import app  # noqa: E402

    doc = app.openapi()
    for item in doc.get("paths", {}).values():
        for op in item.values():
            for param in op.get("parameters", []):
                if param.get("in") == "path" and param.get("schema", {}).get("type") == "integer":
                    param["schema"]["example"] = 1
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"{args.out}: {len(doc.get('paths', {}))} paths")
    if args.seed:
        seed(args.service, args.seed.rstrip("/"))


if __name__ == "__main__":
    main()
