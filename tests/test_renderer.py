import io
from pathlib import Path

import pytest
from PIL import Image, ImageCms

from photo_sync import config, renderer
from photo_sync.common import s3
from photo_sync.models import Slide

HEIC = Path(__file__).parent / "fixtures" / "sample.heic"


def _jpeg(size=(2000, 1500), orientation=None, exif=False):
    img = Image.new("RGB", size, (200, 50, 50))
    buf = io.BytesIO()
    kw = {}
    if orientation or exif:
        e = Image.Exif()
        e[0x0112] = orientation or 1
        e[0x010F] = "Cam"
        kw["exif"] = e.tobytes()
    img.save(buf, "JPEG", **kw)
    return buf.getvalue()


def _px(box, w, h):
    return round((box[2] - box[0]) * w), round((box[3] - box[1]) * h)


@pytest.mark.parametrize("size", [(4000, 3000), (3000, 4000), (1000, 1000), (1001, 777), (50, 3000)])
@pytest.mark.parametrize("box", [[0.1, 0.1, 0.4, 0.3], [0, 0, 1, 1], [0.45, 0.45, 0.55, 0.9], [0.9, 0.9, 1, 1]])
def test_fit_crop_ratio_and_bounds(size, box):
    w, h = size
    out = renderer.fit_crop(box, 0.8, w, h)
    pw, ph = _px(out, w, h)
    assert abs(pw - 0.8 * ph) <= 1
    assert all(0 <= v <= 1 for v in out) and out[0] < out[2] and out[1] < out[3]


def test_fit_crop_center_preserved():
    out = renderer.fit_crop([0.4, 0.4, 0.6, 0.6], 0.8, 4000, 3000)
    assert (out[0] + out[2]) / 2 == pytest.approx(0.5, abs=1 / 4000)
    assert (out[1] + out[3]) / 2 == pytest.approx(0.5, abs=1 / 3000)


def test_fit_crop_full_box_is_largest():
    out = renderer.fit_crop([0, 0, 1, 1], 0.8, 4000, 3000)
    assert _px(out, 4000, 3000) == (2400, 3000)
    out = renderer.fit_crop([0, 0, 1, 1], 0.8, 1000, 3000)
    assert _px(out, 1000, 3000) == (1000, 1250)


@pytest.mark.parametrize("box", [[0.5, 0.5, 0.5, 0.5], [0.7, 0.7, 0.2, 0.2], [-1, -1, 2, 2], [5, 5, 6, 6], [float("nan")] * 4, []])
def test_fit_crop_degenerate(box):
    out = renderer.fit_crop(box, 0.8, 4000, 3000)
    assert all(0 <= v <= 1 for v in out)
    pw, ph = _px(out, 4000, 3000)
    assert ph > 0 and abs(pw - 0.8 * ph) <= 1


def _decode(data):
    img = Image.open(io.BytesIO(data))
    return img


def test_render_jpeg_properties():
    box = renderer.fit_crop([0, 0, 1, 1], 0.8, 2000, 1500)
    data = renderer.render_jpeg(_jpeg(exif=True), box)
    img = _decode(data)
    assert img.format == "JPEG"
    assert max(img.size) <= config.RENDER_MAX_SIDE
    assert abs(img.width / img.height - 0.8) < 0.01
    assert "exif" not in img.info and not img.getexif()
    assert b"Exif" not in data and b"GPS" not in data
    assert img.layer[0][1:3] == (1, 1) and img.layer[1][1:3] == (1, 1)
    prof = ImageCms.ImageCmsProfile(io.BytesIO(img.info["icc_profile"]))
    assert "sRGB" in ImageCms.getProfileDescription(prof)


def test_render_jpeg_no_upscale():
    data = renderer.render_jpeg(_jpeg((400, 500)), [0, 0, 1, 1])
    assert _decode(data).size == (400, 500)


def test_render_jpeg_exif_orientation():
    data = _jpeg((200, 100), orientation=6)
    out = _decode(renderer.render_jpeg(data, [0, 0, 1, 1]))
    assert out.size == (100, 200)


def test_render_jpeg_converts_p3():
    p3 = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (10, 200, 30)).save(buf, "JPEG", icc_profile=p3)
    out = _decode(renderer.render_jpeg(buf.getvalue(), [0, 0, 1, 1]))
    assert out.size == (100, 100)


@pytest.mark.skipif(not HEIC.exists(), reason="no sample.heic")
def test_render_jpeg_heic():
    data = HEIC.read_bytes()
    w, h = renderer._open_upright(data).size
    box = renderer.fit_crop([0.2, 0.2, 0.8, 0.8], 0.8, w, h)
    out = _decode(renderer.render_jpeg(data, box))
    assert out.format == "JPEG" and max(out.size) <= config.RENDER_MAX_SIDE
    assert abs(out.width / out.height - 0.8) < 0.01
    assert not out.getexif()


def test_render_slides(monkeypatch):
    store = {}
    blobs = {"k/a": _jpeg((2000, 1500)), "k/b": _jpeg((1000, 1600))}
    monkeypatch.setattr(s3, "get_object_bytes", lambda k: blobs[k])
    monkeypatch.setattr(s3, "put_object", lambda k, b, ct: store.__setitem__(k, (b, ct)))
    slides = [Slide("a", [0.1, 0.1, 0.3, 0.3], "x"), Slide("b", [0, 0, 1, 1], "y")]
    out = renderer.render_slides("p1", 2, slides, {"a": "k/a", "b": "k/b"})
    assert list(store) == ["renders/p1/v2/01.jpg", "renders/p1/v2/02.jpg"]
    assert all(ct == "image/jpeg" for _, ct in store.values())
    assert [s.render_key for s in out] == list(store)
    assert [s.sha256 for s in out] == ["a", "b"] and [s.alt_text for s in out] == ["x", "y"]
    pw, ph = _px(out[0].crop, 2000, 1500)
    assert abs(pw - 0.8 * ph) <= 1
    assert slides[0].crop == [0.1, 0.1, 0.3, 0.3] and slides[0].render_key is None
    assert out[0] is not slides[0]
