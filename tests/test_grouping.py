import random
from datetime import datetime, timedelta, timezone

from photo_sync import config
from photo_sync.grouping import build_collections
from photo_sync.models import HomeZone, PoolPhoto

ZONES = [HomeZone(10.0, 10.0, 5.0), HomeZone(20.0, 20.0, 5.0)]
HOME = (10.0, 10.0)
AWAY = (30.0, 30.0)
AWAY2 = (31.0, 31.0)
T0 = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
_n = [0]


def ph(t, loc=HOME, **kw):
    _n[0] += 1
    d = dict(
        sha256=f"{_n[0]:064x}", captured_at=t, lat=loc[0] if loc else None, lon=loc[1] if loc else None,
        phash=f"{_n[0] * 0x10101010101:016x}", sharpness=1.0, quality=0.5, blur_score=0.0,
        is_photo=True, is_screenshot=False, is_document=False, is_receipt=False, city=None,
    )
    d.update(kw)
    return PoolPhoto(**d)


def hrs(h):
    return timedelta(hours=h)


def evening(t=T0, n=4, loc=HOME):
    return [ph(t + timedelta(minutes=20 * i), loc) for i in range(n)]


def of_type(cols, t):
    return [c for c in cols if c.type == t]


def test_empty():
    assert build_collections([], ZONES, NOW) == []


def test_event_ready_only_after_24h():
    ps = evening()
    end = ps[-1].captured_at
    c = build_collections(ps, ZONES, end + hrs(23))
    assert len(c) == 1 and c[0].type == "EVENT" and not c[0].ready and not c[0].closed
    c = build_collections(ps, ZONES, end + hrs(24))
    assert c[0].ready and c[0].closed and len(c[0].photo_shas) == 4 and c[0].place is None


def test_event_gap_split():
    a = evening(T0, 3)
    b = evening(T0 + hrs(10), 3)
    assert len(of_type(build_collections(a + b, ZONES, NOW), "EVENT")) == 2


def test_event_radius_split():
    zones = [HomeZone(10.0, 10.1, 50)]
    a = [ph(T0 + timedelta(minutes=20 * i), (10.0, 10.0)) for i in range(3)]
    b = [ph(T0 + timedelta(minutes=120 + 20 * i), (10.0, 10.2)) for i in range(3)]
    assert len(of_type(build_collections(a + b, zones, NOW), "EVENT")) == 2
    near = [ph(T0 + timedelta(minutes=120 + 20 * i), (10.0, 10.01)) for i in range(3)]
    assert len(of_type(build_collections(a + near, zones, NOW), "EVENT")) == 1


def test_trip_closed_by_home_photo():
    trip = [ph(T0 + hrs(i), AWAY, city="Manhattan") for i in range(3)]
    home = ph(T0 + hrs(5), HOME)
    cols = build_collections(trip + [home], ZONES, T0 + hrs(6))
    t = of_type(cols, "TRIP")
    assert len(t) == 1 and t[0].closed and t[0].ready and t[0].place == "Manhattan"
    assert t[0].id.startswith("TRIP#2026-10-10T14:00#")


def test_open_trip_not_ready_and_reserved():
    trip = [ph(T0 + hrs(i), AWAY) for i in range(3)]
    cols = build_collections(trip, ZONES, T0 + hrs(10))
    assert len(cols) == 1 and cols[0].type == "TRIP" and not cols[0].ready and not cols[0].closed
    cols = build_collections(trip, ZONES, T0 + hrs(26))
    assert cols[0].ready


def test_small_trip_released_to_dump():
    trip = [ph(T0 + hrs(i), AWAY) for i in range(2)]
    cols = build_collections(trip, ZONES, NOW)
    assert len(cols) == 1 and cols[0].type == "MONTHLY_DUMP"
    assert set(cols[0].photo_shas) == {p.sha256 for p in trip} and not cols[0].ready
    assert cols[0].id == "MONTHLY_DUMP#2026-10" and cols[0].closed


def test_small_trip_open_stays_reserved():
    trip = [ph(T0 + hrs(i), AWAY) for i in range(2)]
    cols = build_collections(trip, ZONES, T0 + hrs(2))
    assert len(cols) == 1 and cols[0].type == "TRIP" and not cols[0].ready


def test_trip_split_by_home_photo():
    a = [ph(T0 + hrs(i), AWAY) for i in range(3)]
    home = ph(T0 + hrs(4), HOME)
    b = [ph(T0 + hrs(5 + i), AWAY) for i in range(3)]
    cols = build_collections(a + [home] + b, ZONES, NOW)
    assert len(of_type(cols, "TRIP")) == 2


def test_trip_split_by_gap():
    a = [ph(T0 + hrs(i), AWAY) for i in range(3)]
    b = [ph(T0 + hrs(2 + 49 + i), AWAY) for i in range(3)]
    assert len(of_type(build_collections(a + b, ZONES, NOW), "TRIP")) == 2
    b = [ph(T0 + hrs(2 + 48 + i), AWAY) for i in range(3)]
    assert len(of_type(build_collections(a + b, ZONES, NOW), "TRIP")) == 1


def dump_photos(n, month=9):
    return [ph(datetime(2026, month, 1, 12, 0, tzinfo=timezone.utc) + hrs(30 * i), AWAY if i % 2 else (50.0, 50.0)) for i in range(n)]


def test_dump_threshold():
    # unknown-location photos only reach the dump
    def unk(n):
        return [ph(datetime(2026, 9, 1, tzinfo=timezone.utc) + hrs(30 * i), None) for i in range(n)]
    c = build_collections(unk(10), ZONES, NOW)
    assert len(c) == 1 and c[0].ready and c[0].id == "MONTHLY_DUMP#2026-09" and c[0].place is None
    c = build_collections(unk(9), ZONES, NOW)
    assert len(c) == 1 and not c[0].ready and c[0].closed


def test_current_month_excluded():
    ps = [ph(datetime(2026, 11, 2, tzinfo=timezone.utc) + hrs(30 * i), None) for i in range(10)]
    assert build_collections(ps, ZONES, NOW) == []
    edge = [ph(datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc), None)]
    c = build_collections(edge, ZONES, NOW)
    assert c[0].id == "MONTHLY_DUMP#2026-10"
    c = build_collections(ps, ZONES, datetime(2026, 12, 5, tzinfo=timezone.utc))
    assert c[0].id == "MONTHLY_DUMP#2026-11" and c[0].ready


def test_near_duplicate_collapse():
    ps = evening(T0, 3)
    a = ph(T0 + hrs(1), HOME, phash="ff" * 8, sharpness=1.0)
    b = ph(T0 + hrs(1) + timedelta(minutes=5), HOME, phash="ff" * 8, sharpness=9.0)
    c = ph(T0 + hrs(1) + timedelta(minutes=40), HOME, phash="ff" * 8)
    cols = build_collections(ps + [a, b, c], ZONES, NOW)
    col = cols[0]
    assert b.sha256 in col.photo_shas and a.sha256 not in col.photo_shas and c.sha256 in col.photo_shas
    assert col.duplicates == {b.sha256: [a.sha256]}
    assert len(col.photo_shas) == 5
    assert col.end == c.captured_at.astimezone(col.end.tzinfo)


def test_excluded_photos():
    ps = evening(T0, 3)
    bad = [
        ph(T0 + hrs(1), HOME, is_screenshot=True),
        ph(T0 + hrs(1), HOME, is_document=True),
        ph(T0 + hrs(1), HOME, is_receipt=True),
        ph(T0 + hrs(1), HOME, is_photo=False),
        ph(T0 + hrs(1), HOME, blur_score=0.9),
    ]
    cols = build_collections(ps + bad, ZONES, NOW)
    assert len(cols) == 1 and len(cols[0].photo_shas) == 3
    assert not set(cols[0].photo_shas) & {p.sha256 for p in bad}
    assert ph(T0, HOME, blur_score=0.7) and len(build_collections(ps + [ph(T0 + hrs(1), HOME, blur_score=0.7)], ZONES, NOW)[0].photo_shas) == 4


def test_gps_inference_between_outside():
    a = ph(T0, AWAY, city="X")
    mid = ph(T0 + hrs(1), None)
    b = ph(T0 + hrs(2), AWAY2, city="X")
    cols = build_collections([a, mid, b], ZONES, NOW)
    t = of_type(cols, "TRIP")
    assert len(t) == 1 and mid.sha256 in t[0].photo_shas and t[0].place == "X"


def test_gps_inference_single_side():
    a = ph(T0, HOME)
    mid = ph(T0 + hrs(11), None)
    cols = build_collections([a, mid, ph(T0 + hrs(1), HOME)], ZONES, NOW)
    assert mid.sha256 in cols[0].photo_shas


def test_gps_disagreeing_neighbors_unknown():
    a = ph(T0, HOME)
    mid = ph(T0 + hrs(1), None)
    b = ph(T0 + hrs(2), AWAY)
    cols = build_collections([a, mid, b], ZONES, NOW)
    assert len(cols) == 1 and cols[0].type == "MONTHLY_DUMP" and not cols[0].ready
    assert mid.sha256 in cols[0].photo_shas


def test_unknown_does_not_break_or_join_trip():
    a = [ph(T0 + hrs(i), AWAY) for i in range(3)]
    unk = ph(T0 + hrs(20), None)
    b = [ph(T0 + hrs(40 + i), AWAY) for i in range(3)]
    cols = build_collections(a + [unk] + b, ZONES, NOW)
    t = of_type(cols, "TRIP")
    assert len(t) == 1 and unk.sha256 not in t[0].photo_shas
    assert unk.sha256 in of_type(cols, "MONTHLY_DUMP")[0].photo_shas


def test_no_neighbor_within_window():
    a = ph(T0, AWAY)
    far = ph(T0 + hrs(13), None)
    cols = build_collections([a, far], ZONES, NOW)
    assert cols[0].type == "MONTHLY_DUMP" and len(cols[0].photo_shas) == 2
    assert not of_type(cols, "TRIP")


def test_stability_under_shuffle():
    ps = evening(T0, 4) + [ph(T0 + hrs(30 + i), AWAY, city="NYC") for i in range(3)] + dump_photos(10) + [ph(T0 + hrs(40), None)]
    base = build_collections(ps, ZONES, NOW)
    assert len(base) >= 3
    for seed in range(5):
        s = ps[:]
        random.Random(seed).shuffle(s)
        assert build_collections(s, ZONES, NOW) == base
    assert [(c.start, c.id) for c in base] == sorted((c.start, c.id) for c in base)


def test_member_hash_and_id_format():
    ps = evening(T0, 3)
    c = build_collections(ps, ZONES, NOW)[0]
    import hashlib
    assert c.member_hash == hashlib.sha256("\n".join(sorted(p.sha256 for p in ps)).encode()).hexdigest()
    assert c.id == f"EVENT#2026-10-10T14:00#{ps[0].sha256[:8]}"
    assert c.start.utcoffset() is not None


def test_thresholds_read_at_call_time(monkeypatch):
    monkeypatch.setitem(config.MIN_PHOTOS, "EVENT", 5)
    cols = build_collections(evening(T0, 4), ZONES, NOW)
    assert [c.type for c in cols] == ["MONTHLY_DUMP"]
