import pytest

from photo_sync import config
from photo_sync.common import claude
from photo_sync.models import Tags


class Stub:
    def __init__(self, content, stop="tool_use"):
        self.resp = {"output": {"message": {"content": content}}, "stopReason": stop}
        self.kw = None

    def converse(self, **kw):
        self.kw = kw
        return self.resp


def use(monkeypatch, stub):
    monkeypatch.setattr(claude, "_client", lambda: stub)
    return stub


def tool(inp):
    return [{"toolUse": {"name": claude.TOOL, "input": inp}}]


FULL = {k: v for k, v in {
    "tags": ["food"], "scene": "cafe", "mood": "cozy", "quality": 0.8, "people": 0, "food": True,
    "is_photo": True, "is_screenshot": False, "is_document": False, "is_receipt": False,
    "faces_visible": False, "eyes_closed": False, "blur_score": 0.1, "text_visible": False, "notes": "",
}.items()}


def test_tag_photo(monkeypatch):
    stub = use(monkeypatch, Stub(tool(FULL)))
    t = claude.tag_photo(b"jpg")
    assert isinstance(t, Tags) and t.tags == ["food"] and t.food and t.landmark is None
    kw = stub.kw
    assert kw["modelId"] == config.TAGGING_MODEL
    assert kw["messages"][0]["content"][0]["image"] == {"format": "jpeg", "source": {"bytes": b"jpg"}}
    assert kw["toolConfig"]["toolChoice"] == {"tool": {"name": claude.TOOL}}
    assert kw["inferenceConfig"]["maxTokens"] <= 600


def test_schema_matches_tags():
    assert set(claude.SCHEMA["properties"]) == set(Tags.__dataclass_fields__)


@pytest.mark.parametrize(
    "stub",
    [
        Stub([{"text": "hi"}]),
        Stub(tool({"tags": ["x"]})),
        Stub(tool({**FULL, "tags": "notalist"})),
        Stub(tool(FULL), stop="max_tokens"),
    ],
)
def test_malformed(monkeypatch, stub):
    use(monkeypatch, stub)
    with pytest.raises(ValueError):
        claude.tag_photo(b"jpg")
