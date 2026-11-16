from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class HomeZone:
    lat: float
    lon: float
    radius_km: float


@dataclass
class Extracted:
    sha256: str
    width: int
    height: int
    captured_at: str
    utc_offset: str | None
    make: str | None
    model: str | None
    lens_model: str | None
    is_selfie: bool
    lat: float | None
    lon: float | None
    altitude_m: float | None
    speed_kmh: float | None
    direction_deg: float | None
    phash: str
    sharpness: float
    jpeg_1024: bytes
    jpeg_512: bytes
    depth_png: bytes | None


@dataclass
class Place:
    neighborhood: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None


@dataclass
class Tags:
    tags: list[str] = field(default_factory=list)
    scene: str = ""
    mood: str = ""
    quality: float = 0.0
    people: int = 0
    food: bool = False
    landmark: str | None = None
    is_photo: bool = True
    is_screenshot: bool = False
    is_document: bool = False
    is_receipt: bool = False
    faces_visible: bool = False
    eyes_closed: bool = False
    blur_score: float = 0.0
    text_visible: bool = False
    notes: str = ""


@dataclass
class PoolPhoto:
    sha256: str
    captured_at: datetime
    lat: float | None
    lon: float | None
    phash: str
    sharpness: float
    quality: float
    blur_score: float
    is_photo: bool
    is_screenshot: bool
    is_document: bool
    is_receipt: bool
    city: str | None = None
    neighborhood: str | None = None


@dataclass
class Collection:
    id: str
    type: str
    photo_shas: list[str]
    duplicates: dict[str, list[str]]
    start: datetime
    end: datetime
    place: str | None
    member_hash: str
    ready: bool
    closed: bool


@dataclass
class Slide:
    sha256: str
    crop: list[float]
    alt_text: str = ""
    render_key: str | None = None


@dataclass
class Proposal:
    id: str
    collection_id: str
    version: int
    state: str
    post_type: str
    slides: list[Slide]
    captions: list[str]
    chosen_caption: str | None
    place: str | None
    created_at: str
    expires_at: str


@dataclass
class Curation:
    order: list[str]
    crops: dict[str, list[float]]
    alt_text: dict[str, str]
    captions: list[str]


@dataclass
class Intent:
    action: str
    params: dict = field(default_factory=dict)
    critique: dict | None = None
