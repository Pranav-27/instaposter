from datetime import datetime, timezone
from decimal import Decimal

from photo_sync.common import db
from photo_sync.models import Collection
from tests.test_db import table  # noqa: F401


def col(id="c1", **kw):
    base = dict(
        id=id, type="EVENT", photo_shas=["a", "b"], duplicates={"a": ["x"]},
        start=datetime(2026, 11, 1, 10, tzinfo=timezone.utc), end=datetime(2026, 11, 1, 12, tzinfo=timezone.utc),
        place=None, member_hash="h1", ready=True, closed=False,
    )
    return Collection(**{**base, **kw})


T1 = datetime(2026, 11, 2, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 3, tzinfo=timezone.utc)


def test_round_trip_and_created_at(table):
    db.put_collection(col(), "OPEN", T1)
    db.put_collection(col(place="NYC"), "READY", T2)
    item = db.get_collection("c1")
    assert item["status"] == "READY" and item["place"] == "NYC"
    assert item["created_at"] == T1.isoformat() and item["updated_at"] == T2.isoformat()
    assert item["gsi1pk"] == "COLLECTIONS#READY" and item["gsi1sk"] == "2026-11-01T10:00:00+00:00#c1"
    assert item["postable_count"] == 2 and item["photo_shas"] == ["a", "b"] and item["ready"] is True
    assert db.get_collection("nope") is None


def test_place_omitted(table):
    db.put_collection(col(), "OPEN", T1)
    assert "place" not in db.get_collection("c1")


def test_list_and_delete(table):
    db.put_collection(col("a"), "READY", T1)
    db.put_collection(col("b"), "OPEN", T1)
    assert [i["id"] for i in db.list_collections("READY")] == ["a"]
    assert [i["id"] for i in db.list_collections("OPEN")] == ["b"]
    assert db.list_collections("SUPPRESSED") == []
    db.delete_collection("a")
    assert db.get_collection("a") is None and db.list_collections("READY") == []


def pool_item(sha, **kw):
    item = {
        "pk": f"PHOTO#{sha}", "sk": "META", "gsi1pk": "PHOTOS#POOL", "gsi1sk": f"2026-11-01T10:00:00#{sha}",
        "sha256": sha, "captured_at": "2026-11-01T10:00:00-04:00", "phash": "ff",
        "lat": Decimal("10.5"), "lon": Decimal("20.5"), "sharpness": Decimal("100"),
        "tags": {"quality": Decimal("0.7"), "blur_score": Decimal("0.1"), "is_screenshot": True},
        "place": {"city": "NYC", "neighborhood": "SoHo"},
    }
    item.update(kw)
    return {k: v for k, v in item.items() if v is not None}


def test_query_pool_photos(table):
    table.put_item(Item=pool_item("a"))
    table.put_item(Item=pool_item("b", tags=None, place=None, lat=None, lon=None, captured_at="2026-11-01T10:00:00"))
    table.put_item(Item=pool_item("nophash", phash=None))
    table.put_item(Item=pool_item("nocap", captured_at=None))
    table.put_item(Item=pool_item("nosha", sha256=None))
    table.put_item(Item={**pool_item("other"), "gsi1pk": "PHOTOS#POSTED"})
    photos = {p.sha256: p for p in db.query_pool_photos()}
    assert set(photos) == {"a", "b"}
    a, b = photos["a"], photos["b"]
    assert a.captured_at.utcoffset().total_seconds() == -4 * 3600
    assert a.lat == 10.5 and isinstance(a.lat, float) and a.quality == 0.7 and a.blur_score == 0.1
    assert a.is_screenshot and a.is_photo and not a.is_document and not a.is_receipt
    assert a.city == "NYC" and a.neighborhood == "SoHo" and a.sharpness == 100.0
    assert b.captured_at.tzinfo is not None and b.lat is None and b.quality == 0.0 and b.is_photo
    assert b.city is None
