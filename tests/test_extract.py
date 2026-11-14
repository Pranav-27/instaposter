import json
import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image

from photo_sync.extract import _gps, extract

FIXTURE = Path(__file__).parent / "fixtures" / "sample.heic"


@pytest.fixture(scope="module")
def heic():
    if not FIXTURE.exists():
        pytest.skip("sample.heic missing")
    data = FIXTURE.read_bytes()
    return data, extract(data)


def test_heic_fields(heic):
    data, e = heic
    assert e.sha256 == hashlib.sha256(data).hexdigest()
    want = json.loads((FIXTURE.parent / "sample.json").read_text())
    assert (e.width, e.height) == (want["width"], want["height"])
    assert e.lat == pytest.approx(want["lat"], abs=1e-4)
    assert e.lon == pytest.approx(want["lon"], abs=1e-4)
    assert "front TrueDepth" in e.lens_model
    assert e.is_selfie
    assert (e.make, e.model) == (want["make"], want["model"])
    assert e.captured_at == want["captured_at"]
    assert e.utc_offset == want["utc_offset"]
    assert len(e.phash) == 16 and e.sharpness > 0


def test_heic_derivatives(heic):
    _, e = heic
    for blob, side in ((e.jpeg_1024, 1024), (e.jpeg_512, 512)):
        im = Image.open(io.BytesIO(blob))
        assert im.format == "JPEG" and im.mode == "RGB"
        assert (im.height, im.width) == (side, round(2316 * side / 3088))
        assert not im.getexif()
    d = Image.open(io.BytesIO(e.depth_png))
    assert d.format == "PNG" and d.mode == "L"


def test_jpeg_no_exif():
    buf = io.BytesIO()
    Image.new("RGB", (200, 100), "red").save(buf, "JPEG")
    e = extract(buf.getvalue())
    assert (e.width, e.height) == (200, 100)
    assert e.lat is None and e.make is None and e.captured_at == ""
    assert not e.is_selfie and e.depth_png is None


def test_jpeg_orientation_and_exif():
    im = Image.new("RGB", (200, 100), "blue")
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "Acme"
    exif.get_ifd(0x8769)[0x9003] = "2026:07:01 12:00:00"
    exif.get_ifd(0x8769)[0x9011] = "+09:00"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    e = extract(buf.getvalue())
    assert (e.width, e.height) == (100, 200)
    assert e.make == "Acme"
    assert e.captured_at == "2026-06-30T23:00:00-04:00"
    assert e.utc_offset == "+09:00"


def test_naive_time_is_new_york():
    im = Image.new("RGB", (10, 10))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "2026:01:05 08:00:00"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    e = extract(buf.getvalue())
    assert e.captured_at == "2026-01-05T08:00:00-05:00" and e.utc_offset is None


def test_gps_parsing():
    gps = {1: "S", 2: (33.0, 30.0, 0.0), 3: "W", 4: (70.0, 0.0, 36.0), 5: b"\x01", 6: 10.0,
           11: 10.0, 12: "M", 17: 90.0}
    lat, lon, alt, speed, direction = _gps(gps)
    assert lat == pytest.approx(-33.5) and lon == pytest.approx(-70.01)
    assert alt == -10.0 and speed == pytest.approx(16.09344) and direction == 90.0
    assert _gps({}) == (None,) * 5
    assert _gps({11: 10.0, 12: "N"})[3] == pytest.approx(18.52)
