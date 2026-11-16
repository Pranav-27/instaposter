import io
import math

import pillow_heif
from PIL import Image, ImageCms, ImageOps

from photo_sync import config
from photo_sync.common import s3
from photo_sync.models import Slide

pillow_heif.register_heif_opener()

_SRGB = ImageCms.createProfile("sRGB")
_SRGB_BYTES = ImageCms.ImageCmsProfile(_SRGB).tobytes()


def fit_crop(box: list[float], ratio: float, width: int, height: int) -> list[float]:
    max_h = min(height, math.floor(width / ratio + 1e-9))
    try:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in box)
        valid = x1 > x0 and y1 > y0 and all(math.isfinite(float(v)) for v in box)
    except (TypeError, ValueError):
        valid = False
    if valid:
        bw, bh = (x1 - x0) * width, (y1 - y0) * height
        cx, cy = (x0 + x1) / 2 * width, (y0 + y1) / 2 * height
        h = max(bh, bw / ratio)
    else:
        cx, cy, h = width / 2, height / 2, max_h
    h = max(1, min(round(h), max_h))
    w = max(1, min(round(h * ratio), width))
    left = min(max(round(cx - w / 2), 0), width - w)
    top = min(max(round(cy - h / 2), 0), height - h)
    return [left / width, top / height, (left + w) / width, (top + h) / height]


def _open_upright(original: bytes) -> Image.Image:
    src = Image.open(io.BytesIO(original))
    img = src if src.format == "HEIF" else ImageOps.exif_transpose(src)
    img.load()
    return img


def render_jpeg(original: bytes, box: list[float]) -> bytes:
    img = _open_upright(original)
    icc = img.info.get("icc_profile")
    w, h = img.size
    left, top = round(box[0] * w), round(box[1] * h)
    right, bottom = round(box[2] * w), round(box[3] * h)
    img = img.crop((left, top, max(right, left + 1), max(bottom, top + 1)))
    scale = min(1.0, config.RENDER_MAX_SIDE / max(img.size))
    if scale < 1.0:
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    img = img.convert("RGB")
    if icc:
        try:
            img = ImageCms.profileToProfile(
                img,
                ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                _SRGB,
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                outputMode="RGB",
            )
        except ImageCms.PyCMSError:
            pass
    out = io.BytesIO()
    img.save(out, "JPEG", quality=config.RENDER_JPEG_QUALITY, subsampling=0, icc_profile=_SRGB_BYTES)
    return out.getvalue()


def render_slides(proposal_id: str, version: int, slides: list[Slide], originals: dict[str, str]) -> list[Slide]:
    result = []
    for i, slide in enumerate(slides):
        data = s3.get_object_bytes(originals[slide.sha256])
        w, h = _open_upright(data).size
        crop = fit_crop(slide.crop, config.CAROUSEL_RATIO, w, h)
        key = f"renders/{proposal_id}/v{version}/{i + 1:02d}.jpg"
        s3.put_object(key, render_jpeg(data, crop), "image/jpeg")
        result.append(Slide(sha256=slide.sha256, crop=crop, alt_text=slide.alt_text, render_key=key))
    return result
