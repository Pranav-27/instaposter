import pytest

from photo_sync import config, evaluator, flow, renderer
from photo_sync.common import curator_ai, db, s3, telegram
from photo_sync.models import Curation, Extracted, HomeZone, Place, Slide, Tags
from tests.test_db import table  # noqa: F401

NOW = {"now": "2026-11-15T09:00:00-05:00"}
SHAS = ["a1", "b2", "c3", "d4"]
PHASHES = ["0000000000000000", "ffffffffffffffff", "f0f0f0f0f0f0f0f0", "0f0f0f0f0f0f0f0f"]


def put_photo(sha, when, lat, lon, phash, city):
    x = Extracted(
        sha256=sha, width=3000, height=4000, captured_at=when, utc_offset="-05:00", make="A", model="B",
        lens_model="back", is_selfie=False, lat=lat, lon=lon, altitude_m=None, speed_kmh=None,
        direction_deg=None, phash=phash, sharpness=50.0, jpeg_1024=b"", jpeg_512=b"", depth_png=None,
    )
    item = db.build_photo_item(
        x, f"originals/{sha}.heic", lat == 10.0, Place(city=city), Tags(tags=["t"], scene="s", quality=0.8), None
    )
    assert db.put_photo_if_new(item)


@pytest.fixture
def world(table, monkeypatch):  # noqa: F811
    for i, sha in enumerate(SHAS):
        put_photo(sha, f"2026-11-14T{18 + i}:00:00-05:00", 40.7, -74.0, PHASHES[i], "Faraway")
    put_photo("home", "2026-11-15T00:38:00-05:00", 10.0, 20.0, "aaaaaaaaaaaaaaaa", "Homeville")
    monkeypatch.setattr(config, "load_home_zones", lambda: [HomeZone(10.0, 20.0, 1.0)])
    sent, counter = [], iter(range(1000, 2000))

    def message(text, reply_markup=None, reply_to=None):
        sent.append((text, reply_markup))
        return next(counter)

    monkeypatch.setattr(telegram, "send_message", message)
    monkeypatch.setattr(telegram, "send_media_group", lambda jpegs, caption=None: [next(counter) for _ in jpegs])
    monkeypatch.setattr(telegram, "answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "edit_message", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "send_typing", lambda: None)
    monkeypatch.setattr(s3, "get_object_bytes", lambda key: b"jpeg")
    monkeypatch.setattr(
        curator_ai, "curate",
        lambda photos, *a: Curation(
            order=[p["sha256"] for p in photos],
            crops={p["sha256"]: [0, 0, 1, 1] for p in photos},
            alt_text={p["sha256"]: "alt" for p in photos},
            captions=[f"caption {i} #a #b #c" for i in (1, 2, 3)],
        ),
    )
    monkeypatch.setattr(
        renderer, "render_slides",
        lambda pid, v, slides, originals: [Slide(s.sha256, s.crop, s.alt_text, f"renders/{pid}/v{v}/{i}.jpg") for i, s in enumerate(slides)],
    )
    monkeypatch.setattr(
        curator_ai, "summarize_learning",
        lambda pt, ev: {"post_type": pt, "summary": "s", "liked": [e["text"] for e in ev if e["outcome"] == "PICKED"], "disliked": []},
    )
    monkeypatch.setattr(evaluator, "_invoke_curator", lambda cid: flow.start_proposal(cid))
    return sent


def buttons(sent, label):
    return [b["callback_data"] for t, kb in sent if kb for r in kb["inline_keyboard"] for b in r if b["text"] == label]


def test_trip_is_proposed_once_and_approved(world):
    first = evaluator.handler(NOW, None)
    assert first["READY"] == 1 and first["curated"] == 1
    (proposal,) = db.list_proposals("SENT")
    assert proposal.post_type == "TRIP" and len(proposal.slides) == 4
    assert {p["state"] for p in db.get_photos(SHAS)} == {"PROPOSED"}
    assert db.get_photos(["home"])[0]["state"] == "POOL"

    second = evaluator.handler(NOW, None)
    assert second["READY"] == 0 and second["curated"] == 0 and len(db.list_proposals("SENT")) == 1

    flow.handle_callback(buttons(world, "Use this")[0], "cb", None)
    approved = db.get_proposal(proposal.id)
    assert approved.state == "APPROVED" and approved.chosen_caption == "caption 1 #a #b #c"
    flow.handle_callback(buttons(world, "Add these")[0], "cb", None)
    assert [e["text"] for e in db.liked_examples(10)] == ["caption 1 #a #b #c"]


def test_rejected_trip_is_not_reproposed_every_day(world):
    evaluator.handler(NOW, None)
    (proposal,) = db.list_proposals("SENT")
    reject = buttons(world, "Reject")[0]
    flow.handle_callback(reject, "cb", None)
    reasons = [b for b in buttons(world, "Boring")] or buttons(world, "Not interesting")
    flow.handle_callback(reasons[0], "cb", None)
    assert db.get_proposal(proposal.id).state == "REJECTED"
    assert {p["state"] for p in db.get_photos(SHAS)} == {"POOL"}

    again = evaluator.handler(NOW, None)
    assert again["READY"] == 0 and again["curated"] == 0 and again["skipped_suppressed"] == 1
    assert db.list_proposals("SENT") == []


def test_expired_proposal_releases_photos_and_stays_suppressed(world):
    evaluator.handler(NOW, None)
    later = {"now": "2026-11-25T09:00:00-05:00"}
    out = evaluator.handler(later, None)
    assert out["expired"] == 1
    assert {p["state"] for p in db.get_photos(SHAS)} == {"POOL"}
    assert out["curated"] == 0 and db.liked_examples(10) == [] and db.disliked_examples(10) == []
