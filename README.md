# AgriPulse: farm-to-buyer marketplace on AWS (EKS microservices + Lambda + RDS PostgreSQL)

Farmers register, describe their farm (location, size, monthly capacity), list produce and farm resources with their
own prices, and keep farm records. Buyers register and order straight from the farm (pay on delivery), on the web or in
the mobile app. The operations team records incidents, and every incident is reported to Slack.

| Part | What it does | Runs on | Code |
|---|---|---|---|
| **accounts** | Sign-up and sign-in for farmers and buyers; signs the tokens every service checks | EKS | `app/accounts` |
| **farm** | Farmer profiles, listings, farm records, listing photos; stock reservations for the shop | EKS | `app/farm` |
| **shop** | Catalog, carts (Redis), orders: one per farm, pay on delivery | EKS (+ Valkey) | `app/shop` |
| **incidents** | The incident blog: public to read, the operations team writes it; every incident reported to Slack | EKS | `app/incidents` |
| **mobile API** | The mobile app's gateway to accounts, farm and shop | API Gateway + Lambda | `app/mobile-backend` |

Every microservice runs in **staging and production**, each in its own namespace (`shop-staging`, `shop-production`...)
with its own database, all deployed by **Argo CD** from `gitops/`. Each release is a canary that Prometheus judges.

| To | Run | What happens |
|---|---|---|
| **staging** | **app** (pick a service or `all`), or just merge to `main`: only the services that changed are built | test, scan, build, sign -> commit to `gitops/services/<service>/staging.yaml` -> Argo CD syncs `<service>-staging` |
| **production** | **promote** (pick a service or `all`), approve, then **SYNC** in Argo CD | the image staging runs -> commit to `production.yaml` -> `<service>-production` shows OutOfSync and the diff -> you click **SYNC** |

| | |
|---|---|
| Database | ONE **RDS PostgreSQL 18, `db.t3.micro`, Multi-AZ**; a database and an IAM-authenticated user per service and environment |
| Edge | ONE **AWS WAF** (incl. SQL injection) on the ALB (every service, Argo CD, Grafana) and the mobile API; **CloudFront** serves the S3 images |
| Observability | **OpenTelemetry** -> Tempo, Loki; **Prometheus** + **Grafana** (kube-prometheus-stack); CloudWatch metrics via YACE |
| Alerting | **Slack only**: every alert; production alerts also open an incident, which the blog reports to Slack |
| AIOps | **Claude** explains every failed pipeline run in Slack; anomaly alert rules; automated canary analysis; Semgrep rules for AI-written code |
| CI/CD, GitOps | GitHub Actions (OIDC, no keys), Argo CD + Argo Rollouts, Kyverno (signed images only), Dependabot |

![AgriPulse AWS architecture](architecture/agripulse-aws-architecture.png)

| Guide | |
|---|---|
| [docs/AgriPulse-AWS-Deployment-Guide.docx](docs/AgriPulse-AWS-Deployment-Guide.docx) | Architecture, bootstrap, GitHub settings, first deployment, security controls, **AI in this project**, day-to-day operations, troubleshooting, cost, teardown |
| [docs/AgriPulse-App-Guide.docx](docs/AgriPulse-App-Guide.docx) | The services and the mobile gateway: APIs, data, the code file by file, running and testing locally, changing them |
| [docs/AgriPulse-Deployment-Error-Report.docx](docs/AgriPulse-Deployment-Error-Report.docx) | Every error of the first deployment: root cause, fix, and the guard that now prevents it |
| [architecture/](architecture/) | AWS architecture, CI/CD pipelines, request flow with the check at every hop (`.drawio` + PNG) |
| [app/dast/README.md](app/dast/README.md) | OWASP ZAP in the pipelines |

## Endpoints (Route 53 zone `mfon21.space`)

| URL | |
|---|---|
| `https://shop.mfon21.space` | the shop (buyers; farmers buying farm resources) |
| `https://farm.mfon21.space` | the farmer portal |
| `https://accounts.mfon21.space` | register, sign in (one account for web and mobile) |
| `https://incidents.mfon21.space` | the incident blog: anyone can read it; ops accounts declare and update incidents |
| `https://api.mfon21.space` | the mobile API |
| `https://media.mfon21.space` | listing photos (only uploads GuardDuty marked clean) |
| `https://argocd.mfon21.space`, `https://grafana.mfon21.space` | Argo CD and Grafana (their own admin sign-in) |

Staging: the same names under `staging.mfon21.space` (`shop.staging.mfon21.space`...). Every request to a service, Argo CD,
Grafana or the mobile API passes AWS WAF first; only `media.` is CloudFront (S3).

## Where the Claude (Anthropic) API key goes

GitHub > the repository > **Settings > Secrets and variables > Actions > New repository secret**:
**`ANTHROPIC_API_KEY`** = your `sk-ant-...` key. When a workflow run fails, its `diagnose` job sends the redacted
logs of the failed steps to Claude and posts the likely cause and fix to Slack and the run's summary page. The key is
needed only there, never in Terraform or the cluster.

## Deploy (summary)

1. `cd infra && ./bootstrap.sh`: state bucket + the GitHub OIDC role; add the printed `AWS_ROLE_ARN` as a repository secret.
2. Add the secrets `SLACK_WEBHOOK_URL` (required) and `ANTHROPIC_API_KEY`. kubectl admin is `user/levelp` by
   default (`cluster_admin_arns` in `infra/aws/variables.tf`; the optional variable `CLUSTER_ADMIN_ARNS`, a JSON list,
   replaces it), so `aws eks update-kubeconfig --name agripulse-eks --region eu-west-2` just works.
   Scans report only until the variable `SECURITY_GATES` = `enforce`. Create the environments
   `agripulse-infrastructure` and `agripulse-production` with yourself as reviewer.
3. Run **infra-aws**, then **platform**, then **app** (`all`: staging), **promote** (`all`: production), **mobile-backend**.
4. Register at `https://accounts.mfon21.space/register`; grant yourself the ops role to declare and update incidents:
   `kubectl -n accounts-production exec <accounts pod> -- python -m app.admin grant-ops you@example.com`.

Details, checks and costs: [docs/AgriPulse-AWS-Deployment-Guide.docx](docs/AgriPulse-AWS-Deployment-Guide.docx).

## Layout

```text
app/
  accounts/ farm/ shop/ incidents/   the microservices (FastAPI), each with tests and a hash-locked requirements.txt
  common/                            code they share: sign-in tokens, web basics, database, cache, telemetry, Slack
  Dockerfile                         one image recipe: docker build -f app/Dockerfile --build-arg SERVICE=shop app
  mobile-backend/                    the mobile gateway Lambda (standard library only)
  dast/                              OWASP ZAP plans, gate and scan setup
infra/
  bootstrap.sh       once, from a laptop
  aws/               stack 1: composes infra/modules (network, eks, database, registry, storage, secrets,
                     mobile-api, edge, monitoring); the list of services and environments lives here
  platform/          stack 2: composes infra/modules/k8s-* (observability, controllers, policy, database, gitops, edge)
  modules/           one folder per module (main.tf, variables.tf, outputs.tf)
gitops/
  charts/agripulse-service/          the one Helm chart every service uses
  services/<service>/                values.yaml (what the service is), staging.yaml, production.yaml (its image there)
.github/             workflows (infra-aws, platform, app, promote, mobile-backend, destroy),
                     actions, the Terraform preflight and AI diagnosis scripts, Dependabot
architecture/        diagrams: draw.io sources + PNG exports
docs/                the guides and the error report (Word)
```

## Tests (local)

```bash
for s in accounts farm shop incidents; do
  (cd app/$s && pip install --require-hashes -r requirements.txt -r requirements-dev.in && pytest -q)   # 9, 9, 10, 16
done
(cd app/mobile-backend && pip install -r requirements-dev.in && pytest -q)                              # 6
(cd .github/scripts && pytest -q)                                                                        # AI diagnosis, preflight, canary
python -m pytest -q app/dast/tests                                                                       # 12 (DAST gate)
```
