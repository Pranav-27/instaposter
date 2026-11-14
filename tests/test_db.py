from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from photo_sync import config
from photo_sync.common import db
from photo_sync.models import Extracted, Place, Tags


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        db._table.cache_clear()
        boto3.client("dynamodb", region_name=config.REGION).create_table(
            TableName=config.TABLE_NAME,
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk", "gsi1pk", "gsi1sk")],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "gsi1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield db._table()
        db._table.cache_clear()


def extracted(**kw):
    base = dict(
        sha256="abc", width=3024, height=4032, captured_at="2026-11-13T10:00:00", utc_offset="-05:00",
        make="Apple", model="iPhone", lens_model=None, is_selfie=False, lat=10.5, lon=20.5,
        altitude_m=12.5, speed_kmh=None, direction_deg=90.0, phash="ff", sharpness=123.4,
        jpeg_1024=b"", jpeg_512=b"", depth_png=None,
    )
    return Extracted(**{**base, **kw})


def test_build_item():
    item = db.build_photo_item(extracted(), "originals/a.heic", False, Place(city="NYC"), Tags(tags=["a"], quality=0.5), None)
    assert item["pk"] == "PHOTO#abc" and item["sk"] == "META"
    assert item["gsi1pk"] == "PHOTOS#POOL" and item["gsi1sk"] == "2026-11-13T10:00:00#abc"
    assert item["state"] == "POOL" and item["posted"] is False
    assert item["lat"] == Decimal("10.5") and item["tags"]["quality"] == Decimal("0.5")
    assert item["place"] == {"city": "NYC"}
    assert item["jpeg_1024_key"] == "derived/abc/1024.jpg" and item["jpeg_512_key"] == "derived/abc/512.jpg"
    assert "speed_kmh" not in item and "lens_model" not in item and "depth_key" not in item
    assert item["in_home_zone"] is False and "indexed_at" in item


def test_build_item_optional():
    item = db.build_photo_item(extracted(lat=None, lon=None), "k", None, None, Tags(), "derived/abc/depth.png")
    assert "lat" not in item and "place" not in item and "in_home_zone" not in item
    assert item["depth_key"] == "derived/abc/depth.png"
    assert "landmark" not in item["tags"]


def test_put_if_new(table):
    item = db.build_photo_item(extracted(), "k", True, None, Tags(), None)
    assert not db.photo_exists("abc")
    assert db.put_photo_if_new(item) is True
    assert db.photo_exists("abc")
    assert db.put_photo_if_new(item) is False
    res = table.query(
        IndexName="gsi1",
        KeyConditionExpression="gsi1pk = :p",
        ExpressionAttributeValues={":p": "PHOTOS#POOL"},
    )
    assert res["Count"] == 1
