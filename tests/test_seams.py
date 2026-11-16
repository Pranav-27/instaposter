from photo_sync import flow
from photo_sync.common import curator_ai
from tests.test_flow import env, started  # noqa: F401

REAL_INTERPRET = curator_ai.interpret_message
REAL_SUMMARIZE = curator_ai.summarize_learning


def route(monkeypatch, env, action, params, critique=None):
    monkeypatch.setattr(curator_ai, "_call", lambda *a, **k: ({"action": action, "params": params, "critique": critique}, None))
    env.intent = REAL_INTERPRET("msg", {"captions": [], "slide_count": 3, "post_type": "TRIP"}, None)


def test_real_select_intent_approves_that_option(env, monkeypatch):
    pid, _ = started(env)
    route(monkeypatch, env, "select", {"index": 2})
    flow.handle_text("the second", None)
    assert env.proposals[(pid, 1)].state == "APPROVED"


def test_real_remove_intent_creates_new_version(env, monkeypatch):
    pid, _ = started(env)
    route(monkeypatch, env, "remove_photo", {"position": 1})
    flow.handle_text("drop the first", None)
    assert len(env.proposals[(pid, 2)].slides) == 2


def test_real_reorder_intent(env, monkeypatch):
    pid, _ = started(env)
    route(monkeypatch, env, "reorder", {"order": [3, 1, 2]})
    flow.handle_text("3,1,2", None)
    assert env.proposals[(pid, 2)].state == "SENT"


def test_real_learning_summary_shape_is_rendered(env, monkeypatch):
    pid, _ = started(env)
    monkeypatch.setattr(curator_ai, "summarize_learning", REAL_SUMMARIZE)
    monkeypatch.setattr(
        curator_ai, "_call",
        lambda *a, **k: ({"summary": "Prefers dry humor.", "liked": ["kept one"], "disliked": [{"text": "too plain", "reason": "boring"}]}, None),
    )
    flow.handle_callback(f"use|{pid}|1|1", "cb", 1)
    text = env.texts()[-1]
    assert "Prefers dry humor." in text and "kept one" in text and "too plain" in text


def test_home_names_come_from_the_nested_place_map(monkeypatch):
    from photo_sync import config

    monkeypatch.setattr(config, "HOME_PLACE_NAMES", ["Extra"])
    photos = [
        {"in_home_zone": True, "place": {"neighborhood": "Hood", "city": "Town"}},
        {"in_home_zone": False, "place": {"neighborhood": "Away", "city": "Far"}},
        {"in_home_zone": True},
    ]
    assert flow._home_names(photos) == ["Hood", "Town", "Extra"]
