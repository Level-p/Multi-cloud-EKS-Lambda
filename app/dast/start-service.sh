#!/usr/bin/env bash
# Start one AgriPulse service on the CI runner for the DAST full scan, the way it runs in EKS but without AWS:
# SQLite instead of PostgreSQL, a throwaway RSA key pair, real signed tokens (no test-user switch in the code).
#
#   app/dast/start-service.sh <accounts|farm|shop|incidents>          -> http://127.0.0.1:8080
#
# The shop also gets what it calls: a Valkey container (cache, carts) and the farm service on 127.0.0.1:8081.
# Exports for the next steps (GITHUB_ENV): DAST_TOKEN, the bearer token ZAP sends (masked in the logs).
set -euo pipefail
SERVICE="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # app/
LOGS=/tmp/dast-logs; mkdir -p "$LOGS"
VALKEY="docker.io/valkey/valkey:9.2-alpine@sha256:cdcb7b1c7b6f1c9cb9442c71ac6275e78184657448407b2c1fdde7ee8024f4fb"

python -m pip install --quiet --require-hashes -r "$ROOT/$SERVICE/requirements.txt"
[ "$SERVICE" = shop ] && python -m pip install --quiet --require-hashes -r "$ROOT/farm/requirements.txt"
export PYTHONPATH="$ROOT/common"
eval "$(python -m agripulse_common.testing keys)"
export JWT_ISSUER=agripulse-dast ENVIRONMENT=dast COOKIE_SECURE=false CSRF_SECRET=dast-only INTERNAL_TOKEN=dast-only \
  WEBHOOK_TOKEN=dast-only ACCOUNTS_URL=http://127.0.0.1:8080 FARM_URL=http://127.0.0.1:8080 SHOP_URL=http://127.0.0.1:8080 \
  INCIDENTS_URL=http://127.0.0.1:8080 PUBLIC_URL=http://localhost:8080 CURRENCY=NGN

role() { case "$1" in accounts|shop) echo buyer ;; farm) echo farmer ;; incidents) echo ops ;; esac; }
DAST_TOKEN="$(python -m agripulse_common.testing token "$(role "$SERVICE")")"
export DAST_TOKEN

start() {   # <service> <port> <database file>
  (cd "$ROOT/$1" && DATABASE_URL="sqlite:////tmp/dast-$1.db" PYTHONPATH="$ROOT/$1:$ROOT/common" nohup python -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$2" --no-server-header > "$LOGS/$1.log" 2>&1 &)
  for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:$2/healthz" > /dev/null && break; sleep 2; done
  curl -fsS "http://127.0.0.1:$2/readyz"; echo
}

if [ "$SERVICE" = shop ]; then
  docker run -d --name dast-valkey -p 127.0.0.1:6379:6379 "$VALKEY" > /dev/null
  export REDIS_HOST=127.0.0.1 FARM_INTERNAL_URL=http://127.0.0.1:8081
  # another account (a random id, not the buyer's) owns the listing the scan buys: nobody can buy their own
  DAST_FARMER_TOKEN="$(python -c 'import uuid; from agripulse_common.testing import token; print(token("farmer", user_id=str(uuid.uuid4())))')"
  export DAST_FARMER_TOKEN
  start farm 8081
fi
start "$SERVICE" 8080

# The OpenAPI document from the service's own code, and the records behind the example ids
python "$ROOT/dast/export_openapi.py" "$SERVICE" "$ROOT/dast/openapi/$SERVICE.json" --seed http://127.0.0.1:8080

if [ -n "${GITHUB_ENV:-}" ]; then
  echo "::add-mask::$DAST_TOKEN"
  echo "DAST_TOKEN=$DAST_TOKEN" >> "$GITHUB_ENV"
fi
