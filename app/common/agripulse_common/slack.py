"""A report to the operations Slack channel (an incoming webhook URL from Secrets Manager: SLACK_WEBHOOK_URL).

Never raises: a Slack outage must not stop an incident from being recorded. Failures are logged and counted
(agripulse_slack_posts_total{result="error"}), and Prometheus alerts on them.
"""

import json
import logging
import os
import urllib.request

from prometheus_client import Counter

log = logging.getLogger("agripulse.slack")
POSTS = Counter("agripulse_slack_posts_total", "Reports posted to Slack", ["result"])


def post(text: str, blocks: list | None = None, timeout: float = 5.0) -> bool:
    url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not url.startswith("https://hooks.slack.com/"):
        POSTS.labels("disabled").inc()
        return False
    payload = {"text": text, **({"blocks": blocks} if blocks else {})}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            POSTS.labels("ok").inc()
            return True
    except Exception:
        POSTS.labels("error").inc()
        log.warning("posting the report to Slack failed", exc_info=True)
        return False
