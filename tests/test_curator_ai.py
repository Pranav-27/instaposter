import json

import pytest

from photo_sync import config
from photo_sync.common import curator_ai as ca

GOOD = ["Sunset in Lisbon #a #b #c", "Alfama mornings #a #b #c", "Tiles and tram #a #b #c"]
BAD = "Lives at 12 Main St #a #b #c"
HOME = ["Springfield"]


class Stub:
    def __init__(self, *inputs):
        self.inputs = list(inputs)
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw)
        inp = self.inputs.pop(0)
        name = kw["toolConfig"]["toolChoice"]["tool"]["name"]
        return {
            "output": {"message": {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": name, "input": inp}}]}},
            "stopReason": "tool_use",
        }


def use(monkeypatch, *inputs):
    s = Stub(*inputs)
    monkeypatch.setattr(ca, "_client", lambda: s)
    return s


def texts(call):
    return json.dumps([c.get("text") for m in call["messages"] for c in m["content"] if "text" in c])


PHOTOS = [
    {"sha256": "a", "captured_at": "t", "width": 4000, "height": 3000, "tags": {"scene": "x"}, "place": "Lisbon", "is_selfie": False, "jpeg": b"A"},
    {"sha256": "b", "captured_at": "t", "width": 3000, "height": 4000, "tags": {}, "place": None, "is_selfie": True, "jpeg": b"B"},
]


def cur(order=("b", "a"), captions=GOOD, crops=None, alt=None):
    return {
        "order": list(order),
        "crops": crops if crops is not None else {"a": [0.1, 0, 0.9, 1], "b": [0, 0, 1, 1]},
        "alt_text": alt if alt is not None else {"a": "A tram", "b": "A selfie"},
        "captions": captions,
    }


def run(**kw):
    return ca.curate(PHOTOS, "TRIP", "Lisbon", kw.get("liked", []), kw.get("disliked", []), HOME)


def test_curate_happy(monkeypatch):
    s = use(monkeypatch, cur())
    c = run(liked=[{"text": "LIKEDCAP", "post_type": "TRIP"}], disliked=[{"text": "DISLIKEDCAP", "post_type": "TRIP", "critique": "too plain"}])
    assert c.order == ["b", "a"] and c.captions == GOOD and c.crops["a"] == [0.1, 0, 0.9, 1]
    call = s.calls[0]
    assert call["modelId"] == config.CURATOR_MODEL
    assert call["inferenceConfig"]["maxTokens"] <= 2000
    content = call["messages"][0]["content"]
    assert [x["image"]["source"]["bytes"] for x in content if "image" in x] == [b"A", b"B"]
    t = texts(call)
    assert "sha256=a size=4000x3000" in t and "LIKEDCAP" in t and "DISLIKEDCAP" in t and "too plain" in t
    assert "Springfield" in json.dumps(call["system"])


def test_curate_subset_and_missing_crop(monkeypatch):
    use(monkeypatch, cur(order=["a"], crops={}, alt={"a": "x"}))
    c = run()
    assert c.order == ["a"] and c.crops == {"a": [0.0, 0.0, 1.0, 1.0]}


def test_curate_bad_crop_filled(monkeypatch):
    use(monkeypatch, cur(crops={"a": [0.5, 0, 0.2, 1], "b": [0, 0, 2, 1]}))
    assert run().crops == {"a": [0.0, 0.0, 1.0, 1.0], "b": [0.0, 0.0, 1.0, 1.0]}


@pytest.mark.parametrize("bad", [
    cur(order=[]), cur(order=["a", "a"]), cur(order=["a", "zz"]), cur(alt={"a": "x"}),
    cur(captions=GOOD[:2]),
])
def test_curate_invalid(monkeypatch, bad):
    use(monkeypatch, bad, bad)
    with pytest.raises(ValueError):
        run()


def test_curate_retry_then_success(monkeypatch):
    s = use(monkeypatch, cur(captions=[BAD, GOOD[1], GOOD[2]]), cur())
    assert run().captions == GOOD
    assert len(s.calls) == 2
    assert "address" in json.dumps(s.calls[1]["messages"][2:])
    assert sum("image" in x for x in s.calls[1]["messages"][0]["content"]) == 2


def test_curate_retry_exhausted(monkeypatch):
    s = use(monkeypatch, cur(captions=[BAD, GOOD[1], GOOD[2]]), cur(captions=[GOOD[0], GOOD[1], "no tags"]))
    with pytest.raises(ValueError):
        run()
    assert len(s.calls) == 2


def test_curate_missing_tool(monkeypatch):
    class S:
        def converse(self, **kw):
            return {"output": {"message": {"content": [{"text": "hi"}]}}, "stopReason": "end_turn"}

    monkeypatch.setattr(ca, "_client", lambda: S())
    with pytest.raises(ValueError):
        run()


CTX = {"post_type": "TRIP", "place": "Lisbon", "captured_range": "May 1-3", "slides": [{"tags": ["tram"], "scene": "street", "place": "Alfama"}], "cover_jpeg": b"C"}
HIST = [{"text": "OLDCAP", "outcome": "rejected", "critique": "boring", "instruction": "punchier"}]


def gen(**kw):
    return ca.generate_captions(CTX, kw.get("history", HIST), kw.get("instruction"), [{"text": "LIKEDCAP", "post_type": "TRIP"}], [{"text": "DISLIKEDCAP", "post_type": "TRIP", "critique": "meh"}], HOME)


def test_generate_happy(monkeypatch):
    s = use(monkeypatch, {"captions": GOOD})
    assert gen(instruction="too boring, make it funnier") == GOOD
    call = s.calls[0]
    assert call["modelId"] == config.CAPTION_MODEL
    t = texts(call)
    for w in ("OLDCAP", "boring", "punchier", "LIKEDCAP", "DISLIKEDCAP", "meh", "too boring, make it funnier", "Alfama"):
        assert w in t
    assert "Springfield" in json.dumps(call["system"])
    assert any("image" in x for x in call["messages"][0]["content"])


def test_generate_no_cover_no_instruction(monkeypatch):
    s = use(monkeypatch, {"captions": GOOD})
    ca.generate_captions({**CTX, "cover_jpeg": None}, [], None, [], [], HOME)
    c = s.calls[0]["messages"][0]["content"]
    assert not any("image" in x for x in c) and "fresh round" in c[0]["text"]


def test_generate_retry(monkeypatch):
    s = use(monkeypatch, {"captions": [BAD, GOOD[1], GOOD[2]]}, {"captions": GOOD})
    assert gen() == GOOD and len(s.calls) == 2


def test_generate_exhausted(monkeypatch):
    use(monkeypatch, {"captions": [BAD, GOOD[1], GOOD[2]]}, {"captions": [BAD, GOOD[1], GOOD[2]]})
    with pytest.raises(ValueError):
        gen()


def test_generate_malformed(monkeypatch):
    use(monkeypatch, {"captions": "nope"})
    with pytest.raises(ValueError):
        gen()


SUMMARY = {"captions": ["c1", "c2", "c3"], "slide_count": 5, "post_type": "TRIP"}


def interp(monkeypatch, out, text="x", replied=None):
    s = use(monkeypatch, out)
    return ca.interpret_message(text, SUMMARY, replied), s


def test_interpret_generate(monkeypatch):
    i, s = interp(monkeypatch, {"action": "generate_captions", "params": {"instruction": "shorter"}, "critique": {"dimension": "length", "direction": "shorter"}}, "make this shorter", "c2")
    assert i.action == "generate_captions" and i.params == {"instruction": "shorter"} and i.critique == {"dimension": "length", "direction": "shorter"}
    assert s.calls[0]["modelId"] == config.TAGGING_MODEL
    t = texts(s.calls[0])
    assert "c2" in t and "make this shorter" in t and "2. c2" in t


@pytest.mark.parametrize("out,params", [
    ({"action": "select", "params": {"index": 2}}, {"index": 2}),
    ({"action": "edit_text", "params": {"text": "hi"}}, {"text": "hi"}),
    ({"action": "reorder", "params": {"order": [2, 1]}}, {"order": [2, 1]}),
    ({"action": "remove_photo", "params": {"position": 3}}, {"position": 3}),
    ({"action": "reject", "params": {"reason": "photos"}}, {"reason": "photos"}),
    ({"action": "chat", "params": {"reply": "ok"}}, {"reply": "ok"}),
])
def test_interpret_actions(monkeypatch, out, params):
    i, _ = interp(monkeypatch, out)
    assert i.params == params and i.critique is None


@pytest.mark.parametrize("out", [
    {"action": "dance", "params": {}},
    {"action": "select", "params": {}},
    {"action": "select", "params": {"index": 0}},
    {"action": "select", "params": {"index": "2"}},
    {"action": "reorder", "params": {"order": []}},
    {"action": "reorder", "params": {"order": ["a"]}},
    {"action": "reject", "params": {"reason": "bored"}},
    {"action": "generate_captions", "params": {"instruction": "x"}},
    {"action": "generate_captions", "params": {"instruction": "x"}, "critique": {"dimension": "color", "direction": "y"}},
    {"action": "chat", "params": "hi"},
])
def test_interpret_invalid(monkeypatch, out):
    use(monkeypatch, out)
    with pytest.raises(ValueError):
        ca.interpret_message("x", SUMMARY, None)


def test_summarize(monkeypatch):
    s = use(monkeypatch, {"summary": "Likes short.", "liked": ["a"], "disliked": [{"text": "b", "reason": "long"}]})
    events = [{"text": "EVT1", "outcome": "published", "final_text": "EDITED"}, {"text": "EVT2", "outcome": "disliked", "critique": "too long"}]
    r = ca.summarize_learning("TRIP", events)
    assert r == {"post_type": "TRIP", "summary": "Likes short.", "liked": ["a"], "disliked": [{"text": "b", "reason": "long"}]}
    t = texts(s.calls[0])
    assert s.calls[0]["modelId"] == config.CAPTION_MODEL
    assert all(w in t for w in ("EVT1", "EDITED", "EVT2", "too long"))


@pytest.mark.parametrize("out", [
    {"summary": "", "liked": [], "disliked": []},
    {"summary": "x", "liked": [1], "disliked": []},
    {"summary": "x", "liked": [], "disliked": [{"text": "a"}]},
    {"liked": [], "disliked": []},
])
def test_summarize_invalid(monkeypatch, out):
    use(monkeypatch, out)
    with pytest.raises(ValueError):
        ca.summarize_learning("TRIP", [])
