"""Lambda entry point (handler "agripulse_mobile.handler.handler"): API Gateway REST proxy event -> api.handle().

Logs are JSON lines in CloudWatch (the function's log format is JSON), traces go to X-Ray (active tracing,
set in Terraform). Tokens and bodies are never logged.
"""

import base64
import json
import logging

from . import api

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("agripulse.mobile")


def handler(event, context):
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    resp = api.handle(event.get("httpMethod", "GET"), event.get("path") or "/", event.get("headers") or {}, raw,
                      query=api.query_string(event))
    if resp.status >= 500:
        log.error(json.dumps({"path": event.get("resource"), "status": resp.status}))
    return {"statusCode": resp.status, "headers": resp.headers, "body": resp.text()}
