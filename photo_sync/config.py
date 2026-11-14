import json
import os
from functools import lru_cache

import boto3

from photo_sync.models import HomeZone

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("TABLE_NAME", "photo-sync")
BUCKET = os.environ.get("BUCKET", "photo-sync-bucket")
TAGGING_MODEL = os.environ.get("TAGGING_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
INGEST_START_DATE = os.environ.get("INGEST_START_DATE", "2026-11-13")
HOME_ZONES_PARAM = os.environ.get("HOME_ZONES_PARAM", "/photo-sync/home-zones")


@lru_cache
def load_home_zones() -> list[HomeZone]:
    value = boto3.client("ssm", region_name=REGION).get_parameter(Name=HOME_ZONES_PARAM)["Parameter"]["Value"]
    return [HomeZone(**z) for z in json.loads(value)]
