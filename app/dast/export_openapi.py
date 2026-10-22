"""Write the OpenAPI document of a FastAPI app (web or incident-blog) from its own code, and seed
one record so that the scan reaches real pages.

    python app/dast/export_openapi.py web app/dast/openapi/web.json [--seed http://127.0.0.1:8080]

The apps serve no /openapi.json in production (openapi_url=None: less to discover for an
attacker), but FastAPI can still generate the document from the routes and Pydantic models.
ZAP imports it, so the active scan reaches every operation and form field. Because it is
generated, it can never drift from the code.

Integer path ids ({farm_id}, {incident_id}) get the example 1: --seed creates that first record
through the app's own form (with the form token the page would contain), so /farms/1 exists.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
import urllib.request

SEED = {
    "web": ("/farms", {"name": "DAST farm", "region": "Rift Valley", "hectares": "12.5"}),
    "incident-blog": ("/incidents", {"title": "DAST probe incident", "severity": "SEV3", "services": "web",
                                     "summary": "Seeded for the scan."}),
}


def seed(app_name, base):
    user = os.environ["DEV_USER"].lower()
    token = hmac.new(os.environ["CSRF_SECRET"].encode(), user.encode(), hashlib.sha256).hexdigest()
    path, fields = SEED[app_name]
    data = urllib.parse.urlencode({**fields, "csrf": token}).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=10)
    except urllib.error.HTTPError as err:   # 303 See Other is the success answer
        if err.code != 303:
            raise SystemExit(f"seeding {path} failed: HTTP {err.code}")
        print(f"seeded {err.headers.get('Location')}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("app", choices=sorted(SEED))
    p.add_argument("out")
    p.add_argument("--seed", metavar="BASE_URL")
    args = p.parse_args()

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", args.app))   # app/<app>
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
        seed(args.app, args.seed.rstrip("/"))


if __name__ == "__main__":
    main()
