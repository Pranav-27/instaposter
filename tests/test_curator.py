from photo_sync import curator, flow


def test_handler(monkeypatch):
    monkeypatch.setattr(flow, "start_proposal", lambda cid: "p1" if cid == "c" else None)
    assert curator.handler({"collection_id": "c"}, None) == {"proposal_id": "p1"}
