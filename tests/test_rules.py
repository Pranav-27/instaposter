import pytest

from photo_sync.rules import validate_caption

H = "#a #b #c"


def ok(text, home=("Springfield",)):
    return validate_caption(text, list(home))


@pytest.mark.parametrize("text", [
    f"Golden hour in Lisbon {H}",
    f"3 friends and a sunset {H}",
    f"Route 66 vibes {H}",
    f"5 days in Tokyo {H}",
    f"A 10 min drive from Napa {H}",
    f"2 hours on the road {H}",
    f"Top 10 best places {H}",
    f"One way ticket to Rome {H}",
    f"Café time #coffee #rome #love",
])
def test_ok(text):
    assert ok(text) == []


@pytest.mark.parametrize("text", [
    f"Lunch at 123 Main St {H}",
    f"Found 42 Oak Avenue today {H}",
    f"5th Ave shopping 350 5th Ave {H}",
    f"Live at 1600 Pennsylvania Ave. {H}",
    f"Meet at 77 Sunset Boulevard {H}",
    f"10 Downing Street {H}",
    f"221 baker street {H}",
    f"P.O. Box 5 {H}",
    f"PO box 12 {H}",
    f"Send to 9 Maple Ln {H}",
    f"12 Ocean Pkwy {H}",
])
def test_address(text):
    assert any("address" in v for v in ok(text))


def test_hashtag_count():
    assert any("hashtags" in v for v in ok("hi #a #b"))
    assert any("hashtags" in v for v in ok("hi #a #b #c #d"))
    assert validate_caption("hi #a", [], 1) == []
    assert any("hashtags" in v for v in ok("hi"))


def test_home_names():
    assert any("Springfield" in v for v in ok(f"Back in springfield {H}"))
    assert ok(f"Springfields are great {H}") == []
    assert any("Oak Park" in v for v in ok(f"At oak park again {H}", ["Oak Park"]))
    assert ok(f"Hi {H}", [""]) == []


def test_empty_and_length():
    assert ok("") == ["caption is empty"]
    assert validate_caption("   ", []) == ["caption is empty"]
    assert any("2200" in v for v in ok("x" * 2200 + " " + H))
    assert ok("x" * 2000 + " " + H) == []
