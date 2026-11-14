import hashlib
from datetime import datetime, timezone
from urllib.parse import unquote_plus
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from photo_sync import config, geo
from photo_sync import extract as extract_mod
from photo_sync.common import claude, db, places, s3

EXTENSIONS = (".heic", ".jpg", ".jpeg")
NY = ZoneInfo("America/New_York")


def _skip(reason):
    return {"status": "skipped", "reason": reason}


def _fetch(key):
    try:
        return key, s3.get_object_bytes(key)
    except ClientError as e:
        decoded = unquote_plus(key)
        if e.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404") or decoded == key:
            raise
        return decoded, s3.get_object_bytes(decoded)


def _upload_time(event):
    raw = event.get("time")
    t = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else datetime.now(timezone.utc)
    return t.astimezone(NY).isoformat()


def handler(event, context):
    detail = event["detail"]
    key = detail["object"]["key"]
    size = detail["object"].get("size", 0)
    if not key.startswith("originals/"):
        return _skip("prefix")
    if key.endswith("/") or not size:
        return _skip("empty")
    if not key.lower().endswith(EXTENSIONS):
        return _skip("extension")

    s3_key, data = _fetch(key)
    sha = hashlib.sha256(data).hexdigest()
    if db.photo_exists(sha):
        return {"status": "duplicate", "sha256": sha}

    x = extract_mod.extract(data)
    x.captured_at = x.captured_at or _upload_time(event)
    if x.captured_at[:10] < config.INGEST_START_DATE:
        return _skip("before_start_date")

    in_home = place = None
    if x.lat is not None and x.lon is not None:
        in_home = geo.in_home_zone(x.lat, x.lon, config.load_home_zones())
        place = places.reverse_geocode(x.lat, x.lon)

    s3.put_object(s3.derived_key(sha, "1024.jpg"), x.jpeg_1024, "image/jpeg")
    s3.put_object(s3.derived_key(sha, "512.jpg"), x.jpeg_512, "image/jpeg")
    depth_key = None
    if x.depth_png:
        depth_key = s3.derived_key(sha, "depth.png")
        s3.put_object(depth_key, x.depth_png, "image/png")

    tags = claude.tag_photo(x.jpeg_1024)
    item = db.build_photo_item(x, s3_key, in_home, place, tags, depth_key)
    created = db.put_photo_if_new(item)
    return {"status": "indexed" if created else "duplicate", "sha256": sha}
