# DAST: OWASP ZAP

SAST reads the code. **DAST attacks the running service.** Each AgriPulse pipeline scans its service twice:

| Stage | Target | Scan |
|---|---|---|
| `dast` (every PR and main run, before build) | The service started on the runner by `start-service.sh`: uvicorn + SQLite, a throwaway RSA key pair and a **real signed token** (no test-user switch exists in the code). The shop also gets a Valkey container and the farm service it calls. The mobile backend: the real gateway handler behind `mobile_local.py` (API Gateway stand-in, echo services) | **Full** (passive + active), every OpenAPI operation and every form |
| `dast-production` (after the production rollout) | The public URLs, read from the Terraform state (`<service>_url`, `mobile_api_url`) | **Baseline** (passive, unauthenticated): TLS, headers through the ALB (AWS WAF), the sign-in redirect and the session cookie's flags, 401s on protected routes, CORS, error pages |

| File | What it is |
|---|---|
| `zap/ci-full.yaml`, `zap/prod-baseline.yaml` | ZAP Automation Framework plans (one pair for every service) |
| `start-service.sh` | Starts one service for the scan and exports `DAST_TOKEN` (masked) |
| `export_openapi.py` | Writes the service's OpenAPI document from its code (the services serve none in production) and seeds records through its own API, so `/api/listings/1`, `/api/orders/1` and `/incidents/1` exist |
| `openapi/mobile.yaml` | The mobile API contract (API Gateway REST -> the gateway Lambda) |
| `mobile_local.py` | API Gateway + the gateway handler + stand-in services on the runner, no AWS account needed |
| `run-zap.sh` | Runs a plan in the digest-pinned `ghcr.io/zaproxy/zaproxy:2.17.0`, then the gate; GitHub Actions annotations and run summary |
| `zap_gate.py`, `zap-policy.json` | Pass/fail; reviewed, expiring exceptions; JUnit XML and a Markdown summary (on the run's summary page) |
| `tests/` | Unit tests for the gate |

Action: `.github/actions/dast-zap`. Reports: the `zap-<service>-<mode>-<attempt>` artifact of each run.
While the repository variable `SECURITY_GATES` is not `enforce`, findings are reported as warnings and do not fail
the run (the scans still run and upload their reports).

## Triage

A finding that turns out to be a false positive gets a narrow `alertFilter` in `zap/ci-full.yaml` (rule, URL,
parameter, attack) with the reasoning in a comment, reviewed in the PR like code. A real finding that cannot be fixed
at once gets a reviewed, expiring exception in `zap-policy.json` (owner, reason, at most 180 days).

An authenticated production scan would need a dedicated test account in the production accounts service and an
exclusion of every write operation; add it only with the security team's agreement.
