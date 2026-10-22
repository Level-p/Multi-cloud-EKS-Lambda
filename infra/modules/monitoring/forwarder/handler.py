"""CloudWatch alarm -> incident blog. Subscribed to the alarm SNS topic (modules/monitoring).

An alarm that goes to ALARM opens an incident (or adds to the open one for the same alarm) through
POST https://incidents.<zone>/webhooks/cloudwatch with the blog's webhook token as a Bearer header.
OK and INSUFFICIENT_DATA transitions are passed on too (the blog ignores them). Messages that are not
CloudWatch alarms (GuardDuty malware findings from EventBridge, RDS failover events) are reshaped into an
ALARM named after their source, so they open an incident as well. boto3 comes with the Lambda runtime;
the token is read from Secrets Manager once per container.
"""

import json
import os
import urllib.request

import boto3

_token = None


def token() -> str:
    global _token
    if _token is None:
        _token = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["WEBHOOK_TOKEN_SECRET_ARN"])["SecretString"]
    return _token


def as_alarm(subject: str | None, message: str) -> dict:
    """The SNS message as a CloudWatch alarm payload (what the blog's webhook expects)."""
    try:
        payload = json.loads(message)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "NewStateValue" in payload:
        return payload                                       # a CloudWatch alarm already
    if isinstance(payload, dict) and "detail-type" in payload:
        name = f"{payload.get('source', 'aws')}: {payload['detail-type']}"   # EventBridge (GuardDuty malware found)
    elif isinstance(payload, dict) and "Event Message" in payload:
        name = f"RDS {payload.get('Source ID', '')}: {payload.get('Event ID', 'event')}"   # RDS event subscription
    else:
        name = subject or "SNS notification"
    return {"AlarmName": name[:100], "NewStateValue": "ALARM", "AlarmDescription": subject or "",
            "NewStateReason": (payload.get("Event Message") if isinstance(payload, dict) and "Event Message" in payload
                               else message)[:1000]}


def handler(event, context):
    sent = 0
    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        payload = as_alarm(sns.get("Subject"), sns.get("Message", ""))
        req = urllib.request.Request(os.environ["WEBHOOK_URL"], data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token()}",
                                              "User-Agent": "agripulse-alarm-forwarder"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(json.dumps({"alarm": payload.get("AlarmName"), "status": resp.status}))
        sent += 1
    return {"forwarded": sent}
