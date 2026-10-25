#!/usr/bin/env bash
# Run one OWASP ZAP Automation Framework plan in the pinned ZAP container, then the gate.
# Used by .github/actions/dast-zap (GitHub Actions); works on any Linux machine with Docker.
#
#   app/dast/run-zap.sh <plan> <app> <target-url> [spec-file-in-dast/openapi]
#     plan   ci-full | prod-baseline
#     app    accounts | farm | shop | incidents | mobile-backend
#     env    FAIL_ON (optional: low | medium | high; default from app/dast/zap-policy.json)
#            SPEC_SUBST (optional: "PLACEHOLDER=value" applied to the spec before the scan)
# Output in app/dast/reports/: zap-report.{json,sarif,html}, zap-junit.xml, zap-summary.md.
# Exit: 0 pass, 1 findings, 2 the scan itself failed.
set -euo pipefail

PLAN="$1"; APP="$2"; TARGET_URL="$3"; SPEC="${4:-}"
cd "$(dirname "$0")"
ZAP_IMAGE="ghcr.io/zaproxy/zaproxy:2.17.0@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6"

# Error/warning annotations on the GitHub Actions run; plain lines anywhere else
annotate() { if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::$1::$2"; else echo "[$1] $2"; fi; }

rm -rf reports && mkdir -p reports openapi
# Scan a copy of the spec (with the placeholder filled in), never the committed file
SPEC_SUBST="${SPEC_SUBST:-}"
if [ -n "$SPEC" ]; then
  if [ -n "$SPEC_SUBST" ]; then
    sed "s/${SPEC_SUBST%%=*}/${SPEC_SUBST#*=}/g" "openapi/$SPEC" > "openapi/generated-$SPEC"
  else
    cp "openapi/$SPEC" "openapi/generated-$SPEC"
  fi
  SPEC="generated-$SPEC"
fi
chmod -R a+rwX reports openapi   # the container runs as uid 1000 ("zap")

set +e
docker run --rm --network host -v "$PWD:/zap/wrk/dast:rw" \
  -e TARGET_URL="$TARGET_URL" -e DAST_DIR=/zap/wrk/dast -e SPEC="$SPEC" -e APP="$APP" -e DAST_TOKEN="${DAST_TOKEN:-}" \
  "$ZAP_IMAGE" zap.sh -cmd -port 18080 -autorun "/zap/wrk/dast/zap/${PLAN}.yaml" 2>&1 | tee reports/zap.log
ZAP_EXIT=${PIPESTATUS[0]}
set -e
if [ ! -s reports/zap-report.json ]; then
  annotate error "ZAP produced no report (exit $ZAP_EXIT): the plan failed before scanning."
  exit 2
fi
# ZAP exits 1 for plan errors and 2 for warnings. Fail on errors and on failed edge
# assertions (requestor responseCode); report other warnings (e.g. no page at an API's /).
PLAN_FAILED=0
if [ "$ZAP_EXIT" -eq 1 ]; then annotate error "ZAP plan failed (exit 1); see the log."; PLAN_FAILED=1; fi
if grep -q "Difference in response code" reports/zap.log; then
  annotate error "An edge assertion failed (requestor): $(grep -m1 "Difference in response code" reports/zap.log)"; PLAN_FAILED=1
fi
if [ "$ZAP_EXIT" -eq 2 ] && [ "$PLAN_FAILED" -eq 0 ]; then annotate warning "ZAP plan warnings (not blocking); see the log."; fi

GATE=0
python3 zap_gate.py reports/zap-report.json --policy zap-policy.json ${FAIL_ON:+--fail-on "$FAIL_ON"} \
  --title "OWASP ZAP DAST: $APP ($PLAN)" --junit reports/zap-junit.xml --summary reports/zap-summary.md || GATE=$?
# The Markdown summary appears on the run's summary page
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then cat reports/zap-summary.md >> "$GITHUB_STEP_SUMMARY"; fi
[ "$GATE" -eq 1 ] && annotate error "DAST gate failed: see the ZAP summary on the run page and the zap-* artifact."
if [ "$GATE" -eq 0 ] && [ "$PLAN_FAILED" -ne 0 ]; then exit 1; fi
exit "$GATE"
