import boto3
import pytest
from moto import mock_aws

from photo_sync import config
from photo_sync.common import s3


@pytest.fixture
def bucket(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        s3._client.cache_clear()
        boto3.client("s3", region_name=config.REGION).create_bucket(Bucket=config.BUCKET)
        yield
        s3._client.cache_clear()


def test_derived_key():
    assert s3.derived_key("abc", "1024.jpg") == "derived/abc/1024.jpg"


@pytest.mark.parametrize("key", ["originals/a.heic", "originals/IMG 1’s photo.HEIC", "derived/x/depth.png"])
def test_roundtrip(bucket, key):
    s3.put_object(key, b"data", "application/octet-stream")
    assert s3.get_object_bytes(key) == b"data"
    head = boto3.client("s3", region_name=config.REGION).head_object(Bucket=config.BUCKET, Key=key)
    assert head["ContentType"] == "application/octet-stream"
