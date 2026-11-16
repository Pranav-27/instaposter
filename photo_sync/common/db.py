import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from photo_sync import config
from photo_sync.common import s3
from photo_sync.models import Collection, Extracted, Place, PoolPhoto, Proposal, Slide, Tags


@lru_cache
def _table():
    return boto3.resource("dynamodb", region_name=config.REGION).Table(config.TABLE_NAME)


def _clean(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def photo_exists(sha256: str) -> bool:
    return "Item" in _table().get_item(Key={"pk": f"PHOTO#{sha256}", "sk": "META"})


def build_photo_item(
    x: Extracted,
    s3_key: str,
    in_home_zone: bool | None,
    place: Place | None,
    tags: Tags,
    depth_key: str | None,
) -> dict:
    item = {
        "pk": f"PHOTO#{x.sha256}",
        "sk": "META",
        "gsi1pk": "PHOTOS#POOL",
        "gsi1sk": f"{x.captured_at}#{x.sha256}",
        "sha256": x.sha256,
        "s3_key": s3_key,
        "state": "POOL",
        "posted": False,
        "captured_at": x.captured_at,
        "utc_offset": x.utc_offset,
        "lat": x.lat,
        "lon": x.lon,
        "altitude_m": x.altitude_m,
        "speed_kmh": x.speed_kmh,
        "direction_deg": x.direction_deg,
        "in_home_zone": in_home_zone,
        "place": asdict(place) if place else None,
        "make": x.make,
        "model": x.model,
        "lens_model": x.lens_model,
        "is_selfie": x.is_selfie,
        "width": x.width,
        "height": x.height,
        "phash": x.phash,
        "sharpness": x.sharpness,
        "tags": asdict(tags),
        "jpeg_1024_key": s3.derived_key(x.sha256, "1024.jpg"),
        "jpeg_512_key": s3.derived_key(x.sha256, "512.jpg"),
        "depth_key": depth_key,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    return _clean(item)


def put_photo_if_new(item: dict) -> bool:
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _query_gsi1(pk: str) -> list[dict]:
    items, kwargs = [], dict(
        IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(pk)
    )
    while True:
        r = _table().query(**kwargs)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _pool_photo(item: dict) -> PoolPhoto | None:
    if not (item.get("sha256") and item.get("captured_at") and item.get("phash")):
        return None
    try:
        captured = datetime.fromisoformat(item["captured_at"])
    except ValueError:
        return None
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    tags, place = item.get("tags") or {}, item.get("place") or {}

    def num(v, default=0.0):
        return float(v) if v is not None else default

    return PoolPhoto(
        sha256=item["sha256"],
        captured_at=captured,
        lat=num(item.get("lat"), None),
        lon=num(item.get("lon"), None),
        phash=item["phash"],
        sharpness=num(item.get("sharpness")),
        quality=num(tags.get("quality")),
        blur_score=num(tags.get("blur_score")),
        is_photo=bool(tags.get("is_photo", True)),
        is_screenshot=bool(tags.get("is_screenshot", False)),
        is_document=bool(tags.get("is_document", False)),
        is_receipt=bool(tags.get("is_receipt", False)),
        city=place.get("city"),
        neighborhood=place.get("neighborhood"),
    )


def query_pool_photos() -> list[PoolPhoto]:
    photos = (_pool_photo(i) for i in _query_gsi1("PHOTOS#POOL"))
    return [p for p in photos if p]


def get_collection(id: str) -> dict | None:
    return _table().get_item(Key={"pk": f"COLLECTION#{id}", "sk": "META"}).get("Item")


def put_collection(c: Collection, status: str, now: datetime) -> None:
    existing = get_collection(c.id)
    start = c.start.isoformat()
    item = {
        "pk": f"COLLECTION#{c.id}",
        "sk": "META",
        "gsi1pk": f"COLLECTIONS#{status}",
        "gsi1sk": f"{start}#{c.id}",
        "id": c.id,
        "type": c.type,
        "status": status,
        "photo_shas": c.photo_shas,
        "duplicates": c.duplicates,
        "start": start,
        "end": c.end.isoformat(),
        "place": c.place,
        "member_hash": c.member_hash,
        "postable_count": len(c.photo_shas),
        "ready": c.ready,
        "closed": c.closed,
        "created_at": (existing or {}).get("created_at") or now.isoformat(),
        "updated_at": now.isoformat(),
    }
    _table().put_item(Item=_clean(item))


def list_collections(status: str) -> list[dict]:
    return _query_gsi1(f"COLLECTIONS#{status}")


def delete_collection(id: str) -> None:
    _table().delete_item(Key={"pk": f"COLLECTION#{id}", "sk": "META"})


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _conditional(fn) -> bool:
    try:
        fn()
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _proposal_item(p: Proposal) -> dict:
    return _clean(
        {
            "pk": f"PROPOSAL#{p.id}",
            "sk": f"V#{p.version:04d}",
            "gsi1pk": f"PROPOSALS#{p.state}",
            "gsi1sk": f"{p.created_at}#{p.id}",
            **asdict(p),
        }
    )


def _proposal(item: dict) -> Proposal:
    d = _plain(item)
    return Proposal(
        id=d["id"],
        collection_id=d["collection_id"],
        version=d["version"],
        state=d["state"],
        post_type=d["post_type"],
        slides=[Slide(**s) for s in d["slides"]],
        captions=d.get("captions", []),
        chosen_caption=d.get("chosen_caption"),
        place=d.get("place"),
        created_at=d["created_at"],
        expires_at=d["expires_at"],
    )


def put_proposal(p: Proposal) -> None:
    if not _conditional(
        lambda: _table().put_item(Item=_proposal_item(p), ConditionExpression="attribute_not_exists(pk)")
    ):
        raise ValueError(f"proposal {p.id} v{p.version} exists")


def get_proposal(proposal_id: str, version: int | None = None) -> Proposal | None:
    if version is not None:
        item = _table().get_item(Key={"pk": f"PROPOSAL#{proposal_id}", "sk": f"V#{version:04d}"}).get("Item")
    else:
        items = _table().query(
            KeyConditionExpression=Key("pk").eq(f"PROPOSAL#{proposal_id}") & Key("sk").begins_with("V#"),
            ScanIndexForward=False,
            Limit=1,
        )["Items"]
        item = items[0] if items else None
    return _proposal(item) if item else None


def update_proposal(p: Proposal, expected_state: str) -> bool:
    return _conditional(
        lambda: _table().put_item(
            Item=_proposal_item(p),
            ConditionExpression="attribute_exists(pk) AND #s = :e",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":e": expected_state},
        )
    )


def list_proposals(state: str) -> list[Proposal]:
    return [_proposal(i) for i in _query_gsi1(f"PROPOSALS#{state}")]


def set_photo_states(shas: list[str], state: str) -> None:
    for sha in shas:
        _table().update_item(
            Key={"pk": f"PHOTO#{sha}", "sk": "META"},
            UpdateExpression="SET #s = :s, gsi1pk = :g",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":s": state, ":g": f"PHOTOS#{state}"},
        )


def get_photos(shas: list[str]) -> list[dict]:
    name = _table().name
    client = _table().meta.client
    out = []
    for i in range(0, len(shas), 100):
        request = {name: {"Keys": [{"pk": f"PHOTO#{s}", "sk": "META"} for s in dict.fromkeys(shas[i : i + 100])]}}
        while request:
            r = client.batch_get_item(RequestItems=request)
            out += [_plain(x) for x in r["Responses"].get(name, [])]
            request = r.get("UnprocessedKeys") or None
    return out


def set_collection_status(collection_id: str, status: str, expected: str | None = None) -> bool:
    values = {":s": status, ":g": f"COLLECTIONS#{status}"}
    condition = "attribute_exists(pk)"
    if expected is not None:
        condition += " AND #s = :e"
        values[":e"] = expected
    return _conditional(
        lambda: _table().update_item(
            Key={"pk": f"COLLECTION#{collection_id}", "sk": "META"},
            UpdateExpression="SET #s = :s, gsi1pk = :g",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    )


def _caption_key(proposal_id: str, n: int) -> dict:
    return {"pk": f"CAPTION#{proposal_id}", "sk": f"C#{n:04d}"}


def put_caption(proposal_id: str, n: int, text: str, round_no: int, post_type: str, outcome: str = "SHOWN") -> None:
    _table().put_item(
        Item={
            **_caption_key(proposal_id, n),
            "n": n,
            "proposal_id": proposal_id,
            "text": text,
            "round": round_no,
            "post_type": post_type,
            "outcome": outcome,
            "created_at": _now().isoformat(),
            "approved_for_learning": False,
        }
    )


def _set_attrs(key: dict, attrs: dict) -> None:
    attrs = _clean(attrs)
    if not attrs:
        return
    _table().update_item(
        Key=key,
        UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(attrs))),
        ExpressionAttributeNames={f"#k{i}": k for i, k in enumerate(attrs)},
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(attrs.values())},
    )


def update_caption(proposal_id: str, n: int, **attrs) -> None:
    _set_attrs(_caption_key(proposal_id, n), attrs)


def list_captions(proposal_id: str) -> list[dict]:
    items, kwargs = [], dict(
        KeyConditionExpression=Key("pk").eq(f"CAPTION#{proposal_id}") & Key("sk").begins_with("C#")
    )
    while True:
        r = _table().query(**kwargs)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return [_plain(i) for i in items]
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def approve_learning(proposal_id: str) -> int:
    count = 0
    for c in list_captions(proposal_id):
        if c["outcome"] in ("PICKED", "EDITED"):
            polarity = "LIKED"
        elif c["outcome"] in ("CRITIQUED", "REJECTED"):
            polarity = "DISLIKED"
        else:
            continue
        _set_attrs(
            _caption_key(proposal_id, c["n"]),
            {
                "approved_for_learning": True,
                "polarity": polarity,
                "gsi1pk": f"CAPTIONS#{polarity}",
                "gsi1sk": f"{c['created_at']}#{proposal_id}#{c['n']}",
            },
        )
        count += 1
    return count


def _examples(polarity: str, limit: int) -> list[dict]:
    items = _table().query(
        IndexName="gsi1",
        KeyConditionExpression=Key("gsi1pk").eq(f"CAPTIONS#{polarity}"),
        ScanIndexForward=False,
        Limit=limit,
    )["Items"]
    out = []
    for i in map(_plain, items):
        text = (i.get("final_text") or i["text"]) if polarity == "LIKED" else i["text"]
        e = {"text": text, "post_type": i["post_type"], "outcome": i["outcome"]}
        if i.get("critique"):
            e["critique"] = i["critique"]
        out.append(e)
    return out


def liked_examples(limit: int = 10) -> list[dict]:
    return _examples("LIKED", limit)


def disliked_examples(limit: int = 10) -> list[dict]:
    return _examples("DISLIKED", limit)


def put_feedback(proposal_id: str, kind: str, data: dict) -> None:
    now = _now().isoformat()
    _table().put_item(
        Item=_clean(
            {
                "pk": f"FEEDBACK#{proposal_id}",
                "sk": f"{now}#{secrets.token_hex(4)}",
                "kind": kind,
                "data": data,
                "created_at": now,
            }
        )
    )


def list_feedback(proposal_id: str) -> list[dict]:
    r = _table().query(KeyConditionExpression=Key("pk").eq(f"FEEDBACK#{proposal_id}"))
    return [_plain(i) for i in r["Items"]]


def _ttl(**delta) -> int:
    return int((_now() + timedelta(**delta)).timestamp())


def seen_update(update_id: int) -> bool:
    return not _conditional(
        lambda: _table().put_item(
            Item={"pk": f"TGUPDATE#{update_id}", "sk": "META", "ttl": _ttl(days=2)},
            ConditionExpression="attribute_not_exists(pk)",
        )
    )


def put_message_ref(message_id: int, proposal_id: str, version: int, caption_index: int | None = None) -> None:
    _table().put_item(
        Item=_clean(
            {
                "pk": f"TGMSG#{message_id}",
                "sk": "META",
                "proposal_id": proposal_id,
                "version": version,
                "caption_index": caption_index,
                "ttl": _ttl(days=30),
            }
        )
    )


def get_message_ref(message_id: int) -> dict | None:
    item = _table().get_item(Key={"pk": f"TGMSG#{message_id}", "sk": "META"}).get("Item")
    return _plain(item) if item else None


def put_learning_pending(proposal_id: str, summary: dict) -> None:
    _table().put_item(Item=_clean({"pk": f"LEARNING#{proposal_id}", "sk": "META", "summary": summary}))


def get_learning_pending(proposal_id: str) -> dict | None:
    item = _table().get_item(Key={"pk": f"LEARNING#{proposal_id}", "sk": "META"}).get("Item")
    return _plain(item) if item else None


def clear_learning_pending(proposal_id: str) -> None:
    _table().delete_item(Key={"pk": f"LEARNING#{proposal_id}", "sk": "META"})
