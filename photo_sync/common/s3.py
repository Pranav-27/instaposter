from functools import lru_cache

import boto3
from botocore.config import Config

from photo_sync import config


@lru_cache
def _client():
    return boto3.client("s3", region_name=config.REGION, config=Config(retries={"max_attempts": 3}))


def get_object_bytes(key: str) -> bytes:
    return _client().get_object(Bucket=config.BUCKET, Key=key)["Body"].read()


def put_object(key: str, body: bytes, content_type: str) -> None:
    _client().put_object(Bucket=config.BUCKET, Key=key, Body=body, ContentType=content_type)


def derived_key(sha256: str, name: str) -> str:
    return f"derived/{sha256}/{name}"
