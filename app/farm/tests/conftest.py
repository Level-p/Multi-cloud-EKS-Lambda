import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "common")]   # the service, then the shared package

from agripulse_common.testing import test_environment  # noqa: E402

os.environ.update(test_environment(MEDIA_BUCKET="agripulse-media-test", MEDIA_CDN_DOMAIN="media.test", CURRENCY="NGN",
                                   AWS_DEFAULT_REGION="eu-west-2", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test"))
