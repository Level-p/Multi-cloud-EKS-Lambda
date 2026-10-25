import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "common")]   # the service, then the shared package

from agripulse_common.testing import test_environment  # noqa: E402

os.environ.update(test_environment(WEBHOOK_TOKEN="hook", SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T0/B0/test",
                                   PUBLIC_URL="http://incidents.test"))
