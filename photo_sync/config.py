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

MIN_PHOTOS = {"EVENT": 3, "TRIP": 3, "MONTHLY_DUMP": 10}
EVENT_GAP_HOURS = 3
EVENT_RADIUS_KM = 5
EVENT_CLOSE_HOURS = 24
TRIP_CLOSE_HOURS = 24
TRIP_MAX_GAP_HOURS = 48
GPS_INFER_WINDOW_HOURS = 12
MAX_BLUR_SCORE = 0.7
PHASH_MAX_DISTANCE = 6
NEAR_DUP_MINUTES = 15

CURATOR_MODEL = os.environ.get("CURATOR_MODEL", "us.anthropic.claude-opus-4-6-v1")
CAPTION_MODEL = os.environ.get("CAPTION_MODEL", CURATOR_MODEL)
CURATOR_FUNCTION = os.environ.get("CURATOR_FUNCTION", "photo-sync-curator")
BOT_FUNCTION = os.environ.get("BOT_FUNCTION", "photo-sync-bot")
CAROUSEL_RATIO = 0.8
RENDER_MAX_SIDE = 1440
RENDER_JPEG_QUALITY = 95
CAPTIONS_PER_ROUND = 3
CAPTION_EXAMPLES = 10
HASHTAG_COUNT = 3
PROPOSAL_EXPIRY_DAYS = 7
HOME_PLACE_NAMES = [s.strip() for s in os.environ.get("HOME_PLACE_NAMES", "").split(",") if s.strip()]
TELEGRAM_PARAM_PREFIX = os.environ.get("TELEGRAM_PARAM_PREFIX", "/photo-sync/telegram")


@lru_cache
def telegram_secrets() -> dict:
    ssm = boto3.client("ssm", region_name=REGION)
    out = {}
    for key in ("bot-token", "chat-id", "webhook-secret"):
        out[key.replace("-", "_")] = ssm.get_parameter(
            Name=f"{TELEGRAM_PARAM_PREFIX}/{key}", WithDecryption=True
        )["Parameter"]["Value"]
    return out
