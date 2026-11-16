import json
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3

from photo_sync import config, flow, grouping
from photo_sync.common import db


def _invoke_curator(collection_id: str) -> None:
    boto3.client("lambda", region_name=config.REGION).invoke(
        FunctionName=config.CURATOR_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps({"collection_id": collection_id}).encode(),
    )


def handler(event, context):
    event = event or {}
    dry_run = bool(event.get("dry_run"))
    now = datetime.fromisoformat(event["now"]) if event.get("now") else datetime.now(ZoneInfo("America/New_York"))
    photos = db.query_pool_photos()
    collections = grouping.build_collections(photos, config.load_home_zones(), now)
    counts = {"READY": 0, "OPEN": 0}
    locked = suppressed = 0
    ready_ids = []
    for c in collections:
        existing = db.get_collection(c.id)
        status = (existing or {}).get("status")
        if status == "PROPOSED":
            locked += 1
            continue
        if status == "SUPPRESSED" and existing.get("member_hash") == c.member_hash:
            suppressed += 1
            continue
        new_status = "READY" if c.ready else "OPEN"
        counts[new_status] += 1
        if c.ready:
            ready_ids.append(c.id)
        if not dry_run:
            db.put_collection(c, new_status, now)
    produced = {c.id for c in collections}
    stale = [
        i["id"]
        for s in ("READY", "OPEN")
        for i in db.list_collections(s)
        if i["id"] not in produced
    ]
    if not dry_run:
        for id in stale:
            db.delete_collection(id)
    curated = expired = 0
    if not dry_run:
        for id in ready_ids:
            _invoke_curator(id)
            curated += 1
        expired = flow.expire_stale(now)
    return {
        "photos": len(photos),
        **counts,
        "skipped_locked": locked,
        "skipped_suppressed": suppressed,
        "deleted": len(stale),
        "dry_run": dry_run,
        "ready_ids": ready_ids,
        "curated": curated,
        "expired": expired,
    }
