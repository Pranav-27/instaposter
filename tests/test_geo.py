import pytest

from photo_sync.geo import haversine_km, in_home_zone
from photo_sync.models import HomeZone


def test_haversine_zero():
    assert haversine_km(40.7, -74.0, 40.7, -74.0) == 0


def test_haversine_known():
    assert haversine_km(40.7128, -74.0060, 51.5074, -0.1278) == pytest.approx(5570, rel=0.01)


def test_one_degree_latitude():
    assert haversine_km(0, 0, 1, 0) == pytest.approx(111.19, rel=0.001)


def test_in_home_zone():
    zones = [HomeZone(40.0, -74.0, 1.0), HomeZone(41.0, -73.0, 5.0)]
    assert in_home_zone(40.005, -74.0, zones)
    assert in_home_zone(41.03, -73.0, zones)
    assert not in_home_zone(40.5, -74.0, zones)
    assert not in_home_zone(40.0, -74.0, [])
