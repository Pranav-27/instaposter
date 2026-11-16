import pytest

from photo_sync import config, evaluator, flow, grouping
from photo_sync.common import db
from tests.test_db import table  # noqa: F401
from tests.test_db_collections import T1, col

NOW = {"now": "2026-11-14T09:00:00-05:00"}


invoked = []


@pytest.fixture
def run(table, monkeypatch):
    produced = []
    monkeypatch.setattr(grouping, "build_collections", lambda photos, zones, now: list(produced))
    monkeypatch.setattr(config, "load_home_zones", lambda: [])
    monkeypatch.setattr(db, "query_pool_photos", lambda: [object()] * 3)
    invoked.clear()
    monkeypatch.setattr(evaluator, "_invoke_curator", invoked.append)
    monkeypatch.setattr(flow, "expire_stale", lambda now: 2)

    def go(cols, **event):
        produced[:] = cols
        return evaluator.handler({**NOW, **event}, None)

    return go


def statuses():
    return {i["id"]: i["status"] for s in ("READY", "OPEN", "SUPPRESSED", "PROPOSED") for i in db.list_collections(s)}


def test_insert_ready_and_open(run):
    r = run([col("a", ready=True), col("b", ready=False)])
    assert r == {
        "photos": 3, "READY": 1, "OPEN": 1, "skipped_locked": 0, "skipped_suppressed": 0,
        "deleted": 0, "dry_run": False, "ready_ids": ["a"], "curated": 1, "expired": 2,
    }
    assert statuses() == {"a": "READY", "b": "OPEN"}


def test_suppressed_and_proposed(run):
    db.put_collection(col("s"), "SUPPRESSED", T1)
    db.put_collection(col("p"), "PROPOSED", T1)
    r = run([col("s"), col("p")])
    assert r["skipped_suppressed"] == 1 and r["skipped_locked"] == 1 and r["READY"] == 0
    assert statuses() == {"s": "SUPPRESSED", "p": "PROPOSED"}


def test_suppressed_changed_membership_overwritten(run):
    db.put_collection(col("s"), "SUPPRESSED", T1)
    r = run([col("s", member_hash="h2")])
    assert r["READY"] == 1 and r["skipped_suppressed"] == 0
    assert statuses() == {"s": "READY"}
    assert db.get_collection("s")["member_hash"] == "h2"


def test_stale_deleted_but_not_suppressed_or_proposed(run):
    db.put_collection(col("r"), "READY", T1)
    db.put_collection(col("o"), "OPEN", T1)
    db.put_collection(col("s"), "SUPPRESSED", T1)
    db.put_collection(col("p"), "PROPOSED", T1)
    r = run([col("keep")])
    assert r["deleted"] == 2
    assert statuses() == {"keep": "READY", "s": "SUPPRESSED", "p": "PROPOSED"}


def test_dry_run(run):
    db.put_collection(col("old"), "READY", T1)
    r = run([col("a"), col("b", ready=False)], dry_run=True)
    assert r["dry_run"] is True and r["READY"] == 1 and r["OPEN"] == 1 and r["deleted"] == 1
    assert statuses() == {"old": "READY"}


def test_idempotent(run):
    cols = [col("a"), col("b", ready=False)]
    first = run(cols)
    snap = {i: db.get_collection(i)["created_at"] for i in "ab"}
    second = run(cols)
    assert first == second and second["deleted"] == 0
    assert statuses() == {"a": "READY", "b": "OPEN"}
    assert {i: db.get_collection(i)["created_at"] for i in "ab"} == snap


def test_curator_invoked_for_ready_only(run):
    r = run([col("a"), col("b", ready=False)])
    assert invoked == ["a"] and r["curated"] == 1


def test_curator_not_invoked_for_skipped_or_dry_run(run):
    db.put_collection(col("s"), "SUPPRESSED", T1)
    db.put_collection(col("p"), "PROPOSED", T1)
    run([col("s"), col("p")])
    r = run([col("a")], dry_run=True)
    assert invoked == [] and r["curated"] == 0 and r["expired"] == 0
