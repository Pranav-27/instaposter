import hashlib
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from photo_sync import config
from photo_sync.geo import haversine_km, in_home_zone
from photo_sync.models import Collection, HomeZone, PoolPhoto

TZ = ZoneInfo("America/New_York")


def _postable(p: PoolPhoto) -> bool:
    return (
        p.is_photo
        and not (p.is_screenshot or p.is_document or p.is_receipt)
        and p.blur_score <= config.MAX_BLUR_SCORE
    )


def _key(p: PoolPhoto):
    return (p.captured_at, p.sha256)


def _locate(photos, zones):
    window = timedelta(hours=config.GPS_INFER_WINDOW_HOURS)
    gps = sorted((p for p in photos if p.lat is not None and p.lon is not None), key=_key)
    loc = {}
    for p in photos:
        if p.lat is not None and p.lon is not None:
            loc[p.sha256] = (p.lat, p.lon, in_home_zone(p.lat, p.lon, zones), p.city)
            continue
        before = after = None
        for g in gps:
            if g.captured_at <= p.captured_at:
                before = g
            elif after is None:
                after = g
        before = before if before and p.captured_at - before.captured_at <= window else None
        after = after if after and after.captured_at - p.captured_at <= window else None
        ins = {
            id(g): in_home_zone(g.lat, g.lon, zones) for g in (before, after) if g is not None
        }
        if before and after and ins[id(before)] != ins[id(after)]:
            loc[p.sha256] = None
            continue
        if before and after:
            src = before if p.captured_at - before.captured_at <= after.captured_at - p.captured_at else after
        else:
            src = before or after
        loc[p.sha256] = None if src is None else (src.lat, src.lon, ins[id(src)], p.city or src.city)
    return loc


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _dedup(members):
    members = sorted(members, key=_key)
    parent = list(range(len(members)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    gap = timedelta(minutes=config.NEAR_DUP_MINUTES)
    for i, a in enumerate(members):
        for j in range(i + 1, len(members)):
            b = members[j]
            if b.captured_at - a.captured_at > gap:
                break
            if _hamming(a.phash, b.phash) <= config.PHASH_MAX_DISTANCE:
                parent[find(j)] = find(i)
    groups = {}
    for i, m in enumerate(members):
        groups.setdefault(find(i), []).append(m)
    reps, dups = [], {}
    for g in groups.values():
        rep = min(g, key=lambda p: (-p.quality, p.blur_score, -p.sharpness, p.sha256))
        reps.append(rep)
        rest = sorted((p for p in g if p is not rep), key=_key)
        if rest:
            dups[rep.sha256] = [p.sha256 for p in rest]
    return sorted(reps, key=_key), dups


def _make(type_, members, loc, ready_fn, closed, id_override=None):
    reps, dups = _dedup(members)
    shas = [p.sha256 for p in reps]
    ready = len(reps) >= config.MIN_PHOTOS[type_] and closed
    ordered = sorted(members, key=_key)
    start, end = ordered[0].captured_at.astimezone(TZ), ordered[-1].captured_at.astimezone(TZ)
    cities = [
        (p.captured_at, loc[p.sha256][3])
        for p in ordered
        if loc[p.sha256] and not loc[p.sha256][2] and loc[p.sha256][3]
    ]
    counts = Counter(c for _, c in cities)
    latest = {}
    for t, c in cities:
        latest[c] = t
    place = max(counts, key=lambda c: (counts[c], latest[c])) if counts else None
    cid = id_override or f"{type_}#{start.strftime('%Y-%m-%dT%H:%M')}#{shas[0][:8]}"
    return Collection(
        id=cid,
        type=type_,
        photo_shas=shas,
        duplicates=dups,
        start=start,
        end=end,
        place=place,
        member_hash=hashlib.sha256("\n".join(sorted(shas)).encode()).hexdigest(),
        ready=ready,
        closed=closed,
    ), len(reps)


def _events(inside, ):
    clusters, cur, clat, clon = [], [], 0.0, 0.0
    gap = timedelta(hours=config.EVENT_GAP_HOURS)
    for p, (lat, lon) in inside:
        if cur and (
            p.captured_at - cur[-1][0].captured_at > gap
            or haversine_km(lat, lon, clat, clon) > config.EVENT_RADIUS_KM
        ):
            clusters.append([c[0] for c in cur])
            cur = []
        cur.append((p, (lat, lon)))
        clat = sum(c[1][0] for c in cur) / len(cur)
        clon = sum(c[1][1] for c in cur) / len(cur)
    if cur:
        clusters.append([c[0] for c in cur])
    return clusters


def build_collections(photos: list[PoolPhoto], zones: list[HomeZone], now: datetime) -> list[Collection]:
    photos = sorted(photos, key=_key)
    loc = _locate(photos, zones)
    post = [p for p in photos if _postable(p)]
    inside_times = [p.captured_at for p in photos if loc[p.sha256] and loc[p.sha256][2]]
    out = []
    used = set()

    def finish(type_, members, closed):
        c, n = _make(type_, members, loc, None, closed)
        if closed and n < config.MIN_PHOTOS[type_]:
            return
        out.append(c)
        used.update(p.sha256 for p in members)

    inside = [(p, loc[p.sha256][:2]) for p in post if loc[p.sha256] and loc[p.sha256][2]]
    for members in _events(inside):
        closed = now >= members[-1].captured_at + timedelta(hours=config.EVENT_CLOSE_HOURS)
        finish("EVENT", members, closed)

    runs, cur = [], []
    max_gap = timedelta(hours=config.TRIP_MAX_GAP_HOURS)
    for p in post:
        l = loc[p.sha256]
        if not l or l[2]:
            continue
        if cur and (
            p.captured_at - cur[-1].captured_at > max_gap
            or any(cur[-1].captured_at < t < p.captured_at for t in inside_times)
        ):
            runs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        runs.append(cur)
    for members in runs:
        end = members[-1].captured_at
        closed = any(t > end for t in inside_times) or now >= end + timedelta(hours=config.TRIP_CLOSE_HOURS)
        finish("TRIP", members, closed)

    now_month = now.astimezone(TZ).strftime("%Y-%m")
    pool = [
        p for p in post
        if p.sha256 not in used and p.captured_at.astimezone(TZ).strftime("%Y-%m") < now_month
    ]
    if pool:
        latest = max(pool, key=_key).captured_at.astimezone(TZ).strftime("%Y-%m")
        c, _ = _make("MONTHLY_DUMP", pool, loc, None, True, f"MONTHLY_DUMP#{latest}")
        out.append(c)
    return sorted(out, key=lambda c: (c.start, c.id))
