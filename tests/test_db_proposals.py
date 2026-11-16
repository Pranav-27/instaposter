import pytest

from photo_sync.common import db
from datetime import datetime, timezone
from decimal import Decimal

from photo_sync.models import Collection, Proposal, Slide, Tags
from tests.test_db import extracted, table  # noqa: F401


def proposal(id="p1", version=1, state="PENDING", created="2026-11-14T00:00:00"):
    return Proposal(
        id=id, collection_id="c1", version=version, state=state, post_type="carousel",
        slides=[Slide("a", [0.0, 0.1, 1.0, 0.9], "alt", "r/a.jpg")], captions=["x", "y"],
        chosen_caption=None, place="NYC", created_at=created, expires_at="2026-11-20T00:00:00",
    )


def add_photo(sha):
    item = db.build_photo_item(extracted(sha256=sha), f"o/{sha}", False, None, Tags(tags=[], quality=0.5), None)
    assert db.put_photo_if_new(item)


def test_proposal_lifecycle(table):
    p = proposal()
    db.put_proposal(p)
    with pytest.raises(ValueError):
        db.put_proposal(p)
    assert db.get_proposal("p1") == p
    assert db.get_proposal("nope") is None
    p2 = proposal(version=2)
    db.put_proposal(p2)
    assert db.get_proposal("p1").version == 2
    assert db.get_proposal("p1", 1).version == 1
    p2.state, p2.chosen_caption = "APPROVED", "x"
    assert not db.update_proposal(p2, "WRONG")
    assert db.update_proposal(p2, "PENDING")
    assert db.get_proposal("p1").chosen_caption == "x"
    assert [(x.id, x.version) for x in db.list_proposals("PENDING")] == [("p1", 1)]
    assert [x.version for x in db.list_proposals("APPROVED")] == [2]


def test_photo_states_and_get(table):
    add_photo("a")
    add_photo("b")
    db.set_photo_states(["a"], "PROPOSED")
    assert [x.sha256 for x in db.query_pool_photos()] == ["b"]
    assert len(db._query_gsi1("PHOTOS#PROPOSED")) == 1
    photos = db.get_photos(["a", "b", "missing"])
    assert {x["sha256"]: x["state"] for x in photos} == {"a": "PROPOSED", "b": "POOL"}
    assert isinstance(photos[0]["lat"], float)
    assert db.get_photos([]) == []


def test_collection_status(table):
    c = Collection(
        id="c1", type="trip", photo_shas=["a"], duplicates={}, start=datetime(2026, 11, 1, tzinfo=timezone.utc),
        end=datetime(2026, 11, 2, tzinfo=timezone.utc), place="NYC", member_hash="h", ready=True, closed=True,
    )
    db.put_collection(c, "PENDING", datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert not db.set_collection_status("c1", "PROPOSED", expected="DONE")
    assert db.set_collection_status("c1", "PROPOSED", expected="PENDING")
    assert db.set_collection_status("c1", "DONE")
    assert not db.set_collection_status("missing", "DONE")
    item = db.get_collection("c1")
    assert item["status"] == "DONE" and item["member_hash"] == "h"
    assert [i["id"] for i in db.list_collections("DONE")] == ["c1"]
    assert db.list_collections("PENDING") == []


def test_captions_and_learning(table):
    for n, (t, o) in enumerate(
        [("s", "SHOWN"), ("l", "PICKED"), ("e", "EDITED"), ("c", "CRITIQUED"), ("r", "REJECTED")], 1
    ):
        db.put_caption("p1", n, t, 1, "carousel", o)
    db.update_caption("p1", 3, final_text="edited", outcome="EDITED")
    db.update_caption("p1", 4, critique={"too": "long", "score": 0.5})
    caps = db.list_captions("p1")
    assert [c["n"] for c in caps] == [1, 2, 3, 4, 5]
    assert caps[0]["approved_for_learning"] is False and caps[3]["critique"]["score"] == 0.5
    assert db.approve_learning("p1") == 4
    liked = db.liked_examples()
    assert {x["text"] for x in liked} == {"edited", "l"}
    assert len(db.liked_examples(1)) == 1
    disliked = db.disliked_examples()
    assert {x["text"] for x in disliked} == {"c", "r"}
    assert [x for x in disliked if x["outcome"] == "CRITIQUED"][0]["critique"]["too"] == "long"
    assert db.list_captions("p1")[0].get("polarity") is None
    assert db.approve_learning("none") == 0


def test_example_ordering(table):
    db.put_caption("p1", 1, "old", 1, "single", "PICKED")
    db.put_caption("p2", 1, "new", 1, "single", "PICKED")
    db.update_caption("p1", 1, created_at="2026-01-01T00:00:00")
    db.update_caption("p2", 1, created_at="2026-02-01T00:00:00")
    db.approve_learning("p1")
    db.approve_learning("p2")
    assert [x["text"] for x in db.liked_examples()] == ["new", "old"]
    assert [x["text"] for x in db.liked_examples(1)] == ["new"]


def test_feedback(table):
    db.put_feedback("p1", "a", {"n": 1.5})
    db.put_feedback("p1", "b", {})
    db.put_feedback("p2", "c", {})
    fb = db.list_feedback("p1")
    assert [f["kind"] for f in fb] == ["a", "b"] and fb[0]["data"] == {"n": 1.5}


def test_seen_update(table):
    assert db.seen_update(5) is False
    assert db.seen_update(5) is True
    assert db.seen_update(6) is False
    assert isinstance(table.get_item(Key={"pk": "TGUPDATE#5", "sk": "META"})["Item"]["ttl"], Decimal)


def test_message_refs(table):
    db.put_message_ref(1, "p1", 2, 3)
    db.put_message_ref(2, "p1", 2)
    m = db.get_message_ref(1)
    assert (m["proposal_id"], m["version"], m["caption_index"]) == ("p1", 2, 3) and m["ttl"] > 0
    assert "caption_index" not in db.get_message_ref(2)
    assert db.get_message_ref(9) is None


def test_learning_pending(table):
    assert db.get_learning_pending("p1") is None
    db.put_learning_pending("p1", {"likes": ["a"], "score": 0.5})
    assert db.get_learning_pending("p1")["summary"] == {"likes": ["a"], "score": 0.5}
    db.clear_learning_pending("p1")
    assert db.get_learning_pending("p1") is None
