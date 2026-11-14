from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from photo_sync import config
from photo_sync.common import s3
from photo_sync.models import Collection, Extracted, Place, PoolPhoto, Tags


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


def _query_gsi1(pk: str) -> list[dict]:
    items, kwargs = [], dict(
        IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(pk)
    )
    while True:
        r = _table().query(**kwargs)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _pool_photo(item: dict) -> PoolPhoto | None:
    if not (item.get("sha256") and item.get("captured_at") and item.get("phash")):
        return None
    try:
        captured = datetime.fromisoformat(item["captured_at"])
    except ValueError:
        return None
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    tags, place = item.get("tags") or {}, item.get("place") or {}

    def num(v, default=0.0):
        return float(v) if v is not None else default

    return PoolPhoto(
        sha256=item["sha256"],
        captured_at=captured,
        lat=num(item.get("lat"), None),
        lon=num(item.get("lon"), None),
        phash=item["phash"],
        sharpness=num(item.get("sharpness")),
        quality=num(tags.get("quality")),
        blur_score=num(tags.get("blur_score")),
        is_photo=bool(tags.get("is_photo", True)),
        is_screenshot=bool(tags.get("is_screenshot", False)),
        is_document=bool(tags.get("is_document", False)),
        is_receipt=bool(tags.get("is_receipt", False)),
        city=place.get("city"),
        neighborhood=place.get("neighborhood"),
    )


def query_pool_photos() -> list[PoolPhoto]:
    photos = (_pool_photo(i) for i in _query_gsi1("PHOTOS#POOL"))
    return [p for p in photos if p]


def get_collection(id: str) -> dict | None:
    return _table().get_item(Key={"pk": f"COLLECTION#{id}", "sk": "META"}).get("Item")


def put_collection(c: Collection, status: str, now: datetime) -> None:
    existing = get_collection(c.id)
    start = c.start.isoformat()
    item = {
        "pk": f"COLLECTION#{c.id}",
        "sk": "META",
        "gsi1pk": f"COLLECTIONS#{status}",
        "gsi1sk": f"{start}#{c.id}",
        "id": c.id,
        "type": c.type,
        "status": status,
        "photo_shas": c.photo_shas,
        "duplicates": c.duplicates,
        "start": start,
        "end": c.end.isoformat(),
        "place": c.place,
        "member_hash": c.member_hash,
        "postable_count": len(c.photo_shas),
        "ready": c.ready,
        "closed": c.closed,
        "created_at": (existing or {}).get("created_at") or now.isoformat(),
        "updated_at": now.isoformat(),
    }
    _table().put_item(Item=_clean(item))


def list_collections(status: str) -> list[dict]:
    return _query_gsi1(f"COLLECTIONS#{status}")


def delete_collection(id: str) -> None:
    _table().delete_item(Key={"pk": f"COLLECTION#{id}", "sk": "META"})
