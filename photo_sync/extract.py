import hashlib
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import imagehash
import numpy as np
import pillow_heif
from PIL import Image, ImageOps

from photo_sync.models import Extracted

pillow_heif.register_heif_opener()

NY = ZoneInfo("America/New_York")
EXIF_IFD, GPS_IFD = 0x8769, 0x8825
SPEED_TO_KMH = {"K": 1.0, "M": 1.609344, "N": 1.852}


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _str(v) -> str | None:
    if v is None:
        return None
    s = v.decode(errors="ignore") if isinstance(v, bytes) else str(v)
    s = s.strip("\x00 ").strip()
    return s or None


def _dms(value, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    deg = d + m / 60 + s / 3600
    return -deg if _str(ref) in ("S", "W") else deg


def _captured_at(exif, ifd) -> tuple[str, str | None]:
    raw = _str(ifd.get(0x9003)) or _str(ifd.get(0x9004)) or _str(exif.get(0x0132))
    offset = _str(ifd.get(0x9011))
    if not raw:
        return "", offset
    try:
        dt = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return "", offset
    if offset:
        try:
            sign = -1 if offset[0] == "-" else 1
            h, m = offset.lstrip("+-").split(":")
            tz = timezone(sign * timedelta(hours=int(h), minutes=int(m)))
            return dt.replace(tzinfo=tz).astimezone(NY).isoformat(), offset
        except ValueError:
            pass
    return dt.replace(tzinfo=NY).isoformat(), offset


def _gps(gps) -> tuple:
    lat = _dms(gps.get(2), gps.get(1))
    lon = _dms(gps.get(4), gps.get(3))
    if lat is None or lon is None:
        lat = lon = None
    alt = _num(gps.get(6))
    if alt is not None and gps.get(5) in (1, b"\x01"):
        alt = -alt
    speed = _num(gps.get(13))
    if speed is not None:
        speed *= SPEED_TO_KMH.get(_str(gps.get(12)) or "K", 1.0)
    return lat, lon, alt, speed, _num(gps.get(17))


def _sharpness(img: Image.Image) -> float:
    g = np.asarray(img.convert("L"), dtype=np.float64)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4 * g[1:-1, 1:-1]
    return float(lap.var())


def _jpeg(img: Image.Image, max_side: int) -> bytes:
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    copy.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _depth_png(data: bytes) -> bytes | None:
    try:
        depths = pillow_heif.open_heif(io.BytesIO(data)).info.get("depth_images") or []
        if not depths:
            return None
        a = np.asarray(depths[0].to_pillow().convert("F"), dtype=np.float64)
    except Exception:
        return None
    lo, hi = a.min(), a.max()
    a = (a - lo) / (hi - lo) * 255 if hi > lo else np.zeros_like(a)
    buf = io.BytesIO()
    Image.fromarray(a.astype(np.uint8), "L").save(buf, "PNG")
    return buf.getvalue()


def extract(data: bytes) -> Extracted:
    src = Image.open(io.BytesIO(data))
    is_heif = src.format == "HEIF"
    exif = src.getexif()
    ifd, gps = exif.get_ifd(EXIF_IFD), exif.get_ifd(GPS_IFD)
    img = src if is_heif else ImageOps.exif_transpose(src)
    img = img.convert("RGB")

    captured_at, utc_offset = _captured_at(exif, ifd)
    lens = _str(ifd.get(0xA434))
    lat, lon, alt, speed, direction = _gps(gps)
    return Extracted(
        sha256=hashlib.sha256(data).hexdigest(),
        width=img.width,
        height=img.height,
        captured_at=captured_at,
        utc_offset=utc_offset,
        make=_str(exif.get(0x010F)),
        model=_str(exif.get(0x0110)),
        lens_model=lens,
        is_selfie=bool(lens and "front" in lens.lower()),
        lat=lat,
        lon=lon,
        altitude_m=alt,
        speed_kmh=speed,
        direction_deg=direction,
        phash=str(imagehash.phash(img)),
        sharpness=_sharpness(img),
        jpeg_1024=_jpeg(img, 1024),
        jpeg_512=_jpeg(img, 512),
        depth_png=_depth_png(data) if is_heif else None,
    )
