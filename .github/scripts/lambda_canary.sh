#!/bin/bash
# ---------------------------------------------------------------------------
# Canary deployment of the mobile-backend Lambda with automated rollback.
#   1. upload the package, set the handler, publish a new version
#   2. smoke-test the new version directly (GET /health event)
#   3. the "live" alias (behind API Gateway) sends CANARY_WEIGHT of traffic to it
#   4. for BAKE_MINUTES, check every minute: errors of the NEW version (ExecutedVersion
#      dimension) and the live-errors alarm. Any error -> roll back to the old version.
#   5. healthy -> the alias points 100 % to the new version
# Usage: lambda_canary.sh <function-name> <package.zip>
# ---------------------------------------------------------------------------
set -euo pipefail

FN="$1"
ZIP="$2"
ALIAS="${ALIAS:-live}"
HANDLER="${HANDLER:-agripulse_mobile.handler.handler}"
CANARY_WEIGHT="${CANARY_WEIGHT:-0.1}"
BAKE_MINUTES="${BAKE_MINUTES:-5}"
ALARM="${CANARY_ALARM:-agripulse-lambda-live-errors}"

log() { echo "[canary] $*"; }

log "uploading $ZIP to $FN"
aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$ZIP" >/dev/null
aws lambda wait function-updated-v2 --function-name "$FN"
aws lambda update-function-configuration --function-name "$FN" --handler "$HANDLER" >/dev/null
aws lambda wait function-updated-v2 --function-name "$FN"
NEW=$(aws lambda publish-version --function-name "$FN" --description "run ${GITHUB_RUN_NUMBER:-local} ${GITHUB_SHA:0:7}" --query Version --output text)
OLD=$(aws lambda get-alias --function-name "$FN" --name "$ALIAS" --query FunctionVersion --output text)
log "old version $OLD, new version $NEW"

rollback() {
  log "ROLLBACK: $ALIAS stays on version $OLD ($1)"
  aws lambda update-alias --function-name "$FN" --name "$ALIAS" --function-version "$OLD" \
    --routing-config 'AdditionalVersionWeights={}' >/dev/null
  echo "::error::Lambda canary rolled back: $1"
  exit 1
}

# Smoke test of the new version before it gets any user traffic
EVENT='{"resource":"/health","path":"/health","httpMethod":"GET","headers":{},"multiValueHeaders":{},"queryStringParameters":null,"multiValueQueryStringParameters":null,"pathParameters":null,"stageVariables":null,"requestContext":{"requestId":"canary-smoke","stage":"prod","httpMethod":"GET","path":"/health"},"body":null,"isBase64Encoded":false}'
aws lambda invoke --function-name "$FN" --qualifier "$NEW" --cli-binary-format raw-in-base64-out \
  --payload "$EVENT" /tmp/smoke.json >/dev/null
grep -q '"statusCode": *200' /tmp/smoke.json || rollback "smoke test failed: $(head -c 300 /tmp/smoke.json)"
log "smoke test passed"

if [ "$OLD" = "$NEW" ]; then log "nothing new to shift"; exit 0; fi

aws lambda update-alias --function-name "$FN" --name "$ALIAS" --function-version "$OLD" \
  --routing-config "AdditionalVersionWeights={\"$NEW\"=$CANARY_WEIGHT}" >/dev/null
log "canary: $(awk "BEGIN{print $CANARY_WEIGHT*100}")% of live traffic -> version $NEW for $BAKE_MINUTES min"

for i in $(seq 1 "$BAKE_MINUTES"); do
  sleep 60
  START=$(date -u -d "-$((i + 1)) minutes" +%Y-%m-%dT%H:%M:%SZ)
  END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ERRORS=$(aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Errors \
    --dimensions Name=FunctionName,Value="$FN" Name=Resource,Value="$FN:$ALIAS" Name=ExecutedVersion,Value="$NEW" \
    --start-time "$START" --end-time "$END" --period 60 --statistics Sum --query 'sum(Datapoints[].Sum)' --output text)
  STATE=$(aws cloudwatch describe-alarms --alarm-names "$ALARM" --query 'MetricAlarms[0].StateValue' --output text)
  log "minute $i: new-version errors=${ERRORS:-0} alarm=$STATE"
  if [ "${ERRORS:-0}" != "None" ] && awk "BEGIN{exit !(${ERRORS:-0} > 0)}"; then rollback "$ERRORS errors on version $NEW"; fi
  if [ "$STATE" = "ALARM" ]; then rollback "alarm $ALARM in ALARM state"; fi
done

aws lambda update-alias --function-name "$FN" --name "$ALIAS" --function-version "$NEW" \
  --routing-config 'AdditionalVersionWeights={}' >/dev/null
log "promoted: $ALIAS -> version $NEW (100%)"
