import pytest

from photo_sync.common import places
from photo_sync.models import Place


class Stub:
    def __init__(self, items):
        self.items = items
        self.kw = None

    def reverse_geocode(self, **kw):
        self.kw = kw
        return {"ResultItems": self.items}

    geocode = reverse_geocode


def use(monkeypatch, items):
    stub = Stub(items)
    monkeypatch.setattr(places, "_client", lambda: stub)
    return stub


def test_reverse(monkeypatch):
    stub = use(monkeypatch, [{"Address": {
        "SubDistrict": "Sample District", "Locality": "Sample City",
        "Region": {"Name": "Sample Region"}, "Country": {"Name": "Sample Country"}}}])
    assert places.reverse_geocode(10.1, 20.2) == Place("Sample District", "Sample City", "Sample Region", "Sample Country")
    assert stub.kw["QueryPosition"] == [20.2, 10.1]


def test_reverse_partial_and_empty(monkeypatch):
    use(monkeypatch, [{"Address": {"District": "Soho", "Country": {"Name": "US"}}}])
    assert places.reverse_geocode(1, 2) == Place(neighborhood="Soho", country="US")
    use(monkeypatch, [])
    assert places.reverse_geocode(1, 2) == Place()


def test_geocode(monkeypatch):
    stub = use(monkeypatch, [{"Position": [45.6, 12.3]}])
    assert places.geocode("addr") == (12.3, 45.6)
    assert stub.kw["QueryText"] == "addr"


def test_geocode_none(monkeypatch):
    use(monkeypatch, [])
    with pytest.raises(ValueError):
        places.geocode("nowhere")
