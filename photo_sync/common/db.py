from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

from photo_sync import config
from photo_sync.common import s3
from photo_sync.models import Extracted, Place, Tags


@lru_cache
def _table():
    return boto3.resource("dynamodb", region_name=config.REGION).Table(config.TABLE_NAME)


def _clean(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def photo_exists(sha256: str) -> bool:
    return "Item" in _table().get_item(Key={"pk": f"PHOTO#{sha256}", "sk": "META"})


def build_photo_item(
    x: Extracted,
    s3_key: str,
    in_home_zone: bool | None,
    place: Place | None,
    tags: Tags,
    depth_key: str | None,
) -> dict:
    item = {
        "pk": f"PHOTO#{x.sha256}",
        "sk": "META",
        "gsi1pk": "PHOTOS#POOL",
        "gsi1sk": f"{x.captured_at}#{x.sha256}",
        "sha256": x.sha256,
        "s3_key": s3_key,
        "state": "POOL",
        "posted": False,
        "captured_at": x.captured_at,
        "utc_offset": x.utc_offset,
        "lat": x.lat,
        "lon": x.lon,
        "altitude_m": x.altitude_m,
        "speed_kmh": x.speed_kmh,
        "direction_deg": x.direction_deg,
        "in_home_zone": in_home_zone,
        "place": asdict(place) if place else None,
        "make": x.make,
        "model": x.model,
        "lens_model": x.lens_model,
        "is_selfie": x.is_selfie,
        "width": x.width,
        "height": x.height,
        "phash": x.phash,
        "sharpness": x.sharpness,
        "tags": asdict(tags),
        "jpeg_1024_key": s3.derived_key(x.sha256, "1024.jpg"),
        "jpeg_512_key": s3.derived_key(x.sha256, "512.jpg"),
        "depth_key": depth_key,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    return _clean(item)


def put_photo_if_new(item: dict) -> bool:
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True
