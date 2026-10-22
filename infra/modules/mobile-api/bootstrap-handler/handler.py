"""Placeholder deployed by Terraform; the mobile-backend pipeline replaces it with the real package."""
import json


def handler(event, context):
    return {"statusCode": 503, "headers": {"Content-Type": "application/json", "Retry-After": "60"},
            "body": json.dumps({"error": "deployment in progress"})}
