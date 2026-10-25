import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "common")]   # the service, then the shared package

from agripulse_common.testing import test_environment  # noqa: E402

os.environ.update(test_environment(COOKIE_DOMAIN="", INCIDENTS_URL="http://incidents.test"))
