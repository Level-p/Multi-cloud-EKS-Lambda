# DAST: OWASP ZAP

SAST reads the code. **DAST attacks the running application.** Each AgriPulse workflow scans its
app twice:

| Stage | Target | Scan |
|---|---|---|
| `dast` (every PR and main run, before build) | The app started on the runner: web / incident-blog with uvicorn + SQLite and `DEV_USER` in place of oauth2-proxy; mobile-backend's real request handler behind `mobile_local.py` (API Gateway stand-in, SQLite for PostgreSQL, moto for S3) | **Full** (passive + active), every OpenAPI operation and every form |
| `dast-production` (after observe / deploy) | The public URLs, read from the Terraform state (`web_url`, `incident_blog_url`, `mobile_api_url`) | **Baseline** (passive, unauthenticated): TLS, headers after CloudFront (AWS WAF), sign-in redirects and cookies, 401s on protected routes, CORS, error pages |

| File | What it is |
|---|---|
| `zap/ci-full.yaml`, `zap/prod-baseline.yaml` | ZAP Automation Framework plans (one pair for all three apps) |
| `export_openapi.py` | Writes the FastAPI app's OpenAPI document from its code (the apps serve none in production) and seeds one record through the app's own form, so `/farms/1` and `/incidents/1` exist |
| `openapi/mobile.yaml` | The mobile API contract (API Gateway REST -> Lambda) |
| `mobile_local.py` | API Gateway + Lambda handler + SQLite + S3 (moto) on the runner, no AWS account needed |
| `run-zap.sh` | Runs a plan in the digest-pinned `ghcr.io/zaproxy/zaproxy:2.17.0`, then the gate; GitHub Actions annotations and run summary |
| `zap_gate.py`, `zap-policy.json` | Pass/fail; reviewed, expiring exceptions; JUnit XML and a Markdown summary (on the run's summary page) |
| `tests/` | Unit tests for the gate |

Action: `.github/actions/dast-zap`. Reports: the `zap-<app>-<mode>-<attempt>` artifact of each run.
Nothing to configure: the production URLs come from the Terraform outputs (`.github/actions/infra-outputs`).

## Triage example (from the first scan)

ZAP reported **SQL Injection (High)** on `GET /api/prices?crop=%`. Reproduced with curl:
`crop=%` gives `400 invalid crop` (rejected by the allow-list regex), and `crop=XYZABCDEFGHIJ` gives `200 {"prices": []}`.
ZAP's boolean check saw two different responses and took that as injection. The query is a bound
SQLAlchemy parameter (`MarketPrice.crop == crop`) with no `LIKE`, so it is a **false positive**, recorded in
`zap/ci-full.yaml` as a narrow `alertFilter` (rule, URL, parameter, attack) with this reasoning.

An authenticated production scan would need a dedicated test identity in both Cognito pools
(and an exclusion of write operations); add it only with the security team's agreement.
