import hashlib

import pytest
from botocore.exceptions import ClientError

from photo_sync import config, indexer
from photo_sync.common import claude, db, places, s3
from photo_sync import extract as extract_mod, geo
from photo_sync.models import Extracted, Place, Tags

KEY = "originals/Test’s iphone/Recents/2026-11-13_13-44-30_IMG_0732.HEIC"
DATA = b"photo-bytes"
SHA = hashlib.sha256(DATA).hexdigest()


def event(key=KEY, size=100):
    return {"detail": {"bucket": {"name": "b"}, "object": {"key": key, "size": size}}}


def make_x(**kw):
    base = dict(
        sha256=SHA, width=1, height=1, captured_at="2026-11-13T13:44:30", utc_offset=None,
        make=None, model=None, lens_model=None, is_selfie=False, lat=1.0, lon=2.0,
        altitude_m=None, speed_kmh=None, direction_deg=None, phash="p", sharpness=1.0,
        jpeg_1024=b"j1024", jpeg_512=b"j512", depth_png=b"depth",
    )
    base.update(kw)
    return Extracted(**base)


@pytest.fixture
def env(monkeypatch):
    s = {"puts": [], "fetched": [], "items": [], "exists": False, "created": True, "x": make_x(), "objects": {KEY: DATA}}

    def get(key):
        s["fetched"].append(key)
        if key not in s["objects"]:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return s["objects"][key]

    monkeypatch.setattr(s3, "get_object_bytes", get)
    monkeypatch.setattr(s3, "put_object", lambda k, b, c: s["puts"].append((k, b, c)))
    monkeypatch.setattr(s3, "derived_key", lambda sha, name: f"derived/{sha}/{name}")
    monkeypatch.setattr(db, "photo_exists", lambda sha: s["exists"])
    monkeypatch.setattr(db, "put_photo_if_new", lambda item: s["items"].append(item) or s["created"])
    monkeypatch.setattr(
        db, "build_photo_item",
        lambda x, k, h, p, t, d: {"key": k, "home": h, "place": p, "tags": t, "depth": d},
    )
    monkeypatch.setattr(extract_mod, "extract", lambda data: s["x"])
    monkeypatch.setattr(geo, "in_home_zone", lambda lat, lon, zones: True)
    monkeypatch.setattr(config, "load_home_zones", lambda: [])
    monkeypatch.setattr(places, "reverse_geocode", lambda lat, lon: Place(city="X"))
    monkeypatch.setattr(claude, "tag_photo", lambda b: Tags(tags=["a"]))
    return s


def test_indexed(env):
    r = indexer.handler(event(), None)
    assert r == {"status": "indexed", "sha256": SHA}
    item = env["items"][0]
    assert item["key"] == KEY and item["home"] is True and item["place"].city == "X"
    assert [p[0] for p in env["puts"]] == [f"derived/{SHA}/{n}" for n in ("1024.jpg", "512.jpg", "depth.png")]
    assert [p[2] for p in env["puts"]] == ["image/jpeg", "image/jpeg", "image/png"]


def test_duplicate_before_extract(env, monkeypatch):
    env["exists"] = True
    monkeypatch.setattr(extract_mod, "extract", lambda d: pytest.fail("paid call"))
    assert indexer.handler(event(), None)["status"] == "duplicate"
    assert not env["puts"]


def test_duplicate_on_conditional_put(env):
    env["created"] = False
    assert indexer.handler(event(), None)["status"] == "duplicate"


@pytest.mark.parametrize(
    "key,size,reason",
    [
        ("originals/folder/", 0, "empty"),
        (KEY, 0, "empty"),
        ("originals/a.png", 5, "extension"),
        ("other/a.jpg", 5, "prefix"),
    ],
)
def test_skips(env, key, size, reason):
    assert indexer.handler(event(key, size), None) == {"status": "skipped", "reason": reason}
    assert not env["fetched"]


def test_before_start_date(env, monkeypatch):
    monkeypatch.setattr(config, "INGEST_START_DATE", "2026-11-14")
    r = indexer.handler(event(), None)
    assert r == {"status": "skipped", "reason": "before_start_date"}
    assert not env["puts"] and not env["items"]


def test_no_gps(env, monkeypatch):
    env["x"] = make_x(lat=None, lon=None)
    monkeypatch.setattr(places, "reverse_geocode", lambda *a: pytest.fail("geocode"))
    monkeypatch.setattr(geo, "in_home_zone", lambda *a: pytest.fail("zone"))
    assert indexer.handler(event(), None)["status"] == "indexed"
    assert env["items"][0]["home"] is None and env["items"][0]["place"] is None


def test_encoded_key_fallback(env):
    raw = "originals/Test%E2%80%99s+iphone/a%20b.jpg"
    decoded = "originals/Test’s iphone/a b.jpg"
    env["objects"] = {decoded: DATA}
    assert indexer.handler(event(raw), None)["status"] == "indexed"
    assert env["fetched"] == [raw, decoded]
    assert env["items"][0]["key"] == decoded


def test_other_errors_propagate(env):
    env["objects"] = {}
    with pytest.raises(ClientError):
        indexer.handler(event("originals/x.jpg"), None)


def test_uppercase_extension(env):
    env["objects"]["originals/A.JPEG"] = DATA
    assert indexer.handler(event("originals/A.JPEG"), None)["status"] == "indexed"


def test_depth_absent(env):
    env["x"] = make_x(depth_png=None)
    indexer.handler(event(), None)
    assert len(env["puts"]) == 2 and env["items"][0]["depth"] is None


def test_depth_present(env):
    indexer.handler(event(), None)
    assert env["items"][0]["depth"] == f"derived/{SHA}/depth.png"


def test_missing_timestamp_falls_back_to_upload_time(env):
    env["x"] = make_x(captured_at="")
    ev = event()
    ev["time"] = "2026-11-13T21:45:53Z"
    assert indexer.handler(ev, None)["status"] == "indexed"
    assert env["x"].captured_at == "2026-11-13T16:45:53-05:00"
