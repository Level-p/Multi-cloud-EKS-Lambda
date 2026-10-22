import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent.parent))   # app/mobile-backend: agripulse_mobile
os.environ.update(AWS_DEFAULT_REGION="eu-west-2", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test")

from agripulse_mobile import api  # noqa: E402
from agripulse_mobile.db import make_session_factory  # noqa: E402

REGION = "eu-west-2"
ORIGIN_SECRET = "origin-secret-for-tests"


@pytest.fixture(autouse=True)
def deps():
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="agripulse-media-test", CreateBucketConfiguration={"LocationConstraint": REGION})
        api.configure(api.Dependencies(sessions=make_session_factory("sqlite://"), s3=s3, media_bucket="agripulse-media-test",
                                       media_cdn_domain="media.example.com", origin_secret=ORIGIN_SECRET))
        yield
        api.configure(None)


def call(method, path, body=None, user="farmer-1", origin=ORIGIN_SECRET):
    """user: the Cognito "sub" the API Gateway authorizer would pass (None = no valid token)."""
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["X-Origin-Verify"] = origin
    resp = api.handle(method, path, headers, json.dumps(body).encode() if body is not None else b"", user=user)
    return resp.status, resp.body
