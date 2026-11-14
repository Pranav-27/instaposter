from functools import lru_cache

import boto3
from botocore.config import Config

from photo_sync import config
from photo_sync.models import Place


@lru_cache
def _client():
    return boto3.client("geo-places", region_name=config.REGION, config=Config(retries={"max_attempts": 3}))


def reverse_geocode(lat: float, lon: float) -> Place:
    items = _client().reverse_geocode(QueryPosition=[lon, lat], MaxResults=1).get("ResultItems", [])
    if not items:
        return Place()
    a = items[0].get("Address", {})
    return Place(
        neighborhood=a.get("SubDistrict") or a.get("District"),
        city=a.get("Locality"),
        region=a.get("Region", {}).get("Name"),
        country=a.get("Country", {}).get("Name"),
    )


def geocode(address: str) -> tuple[float, float]:
    items = _client().geocode(QueryText=address, MaxResults=1).get("ResultItems", [])
    if not items:
        raise ValueError(f"no geocode result for {address!r}")
    lon, lat = items[0]["Position"]
    return lat, lon
