"""Lambda entry point (handler "agripulse_mobile.handler.handler"): API Gateway REST proxy event -> api.handle().

The Cognito authorizer has already verified the token on every route but /health and put its claims in
requestContext.authorizer.claims; the farmer's id is "sub". Logs are JSON lines in CloudWatch (the
function's log format is JSON), traces go to X-Ray (active tracing, set in Terraform).
"""

import base64
import json
import logging

from . import api

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("agripulse.mobile")


def handler(event, context):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    resp = api.handle(event.get("httpMethod", "GET"), event.get("path") or "/", event.get("headers") or {}, raw,
                      user=claims.get("sub"))
    if resp.status >= 500:
        log.error(json.dumps({"path": event.get("resource"), "status": resp.status}))
    return {"statusCode": resp.status, "headers": resp.headers, "body": resp.text()}
