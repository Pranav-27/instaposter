from math import asin, cos, radians, sin, sqrt

from photo_sync.models import HomeZone

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    a = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def in_home_zone(lat: float, lon: float, zones: list[HomeZone]) -> bool:
    return any(haversine_km(lat, lon, z.lat, z.lon) <= z.radius_km for z in zones)
