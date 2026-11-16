from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from photo_sync import config, flow, renderer, rules
from photo_sync.common import curator_ai, db, s3, telegram
from photo_sync.models import Curation, Intent, Proposal, Slide

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


class Env:
    def __init__(self, mp, n_photos=3):
        self.log = []
        self.sent = []
        self.answers = []
        self.edits = []
        self.proposals = {}
        self.captions = {}
        self.feedback = []
        self.refs = {}
        self.pending = {}
        self.photo_states = {}
        self.collection = {"id": "c1", "type": "TRIP", "place": "Lisbon", "photo_shas": [f"s{i}" for i in range(n_photos)]}
        self.status = {"c1": "READY"}
        self.fail_send = False
        self.intent = Intent("chat", {"reply": "hi"})
        self.violations = []
        self.gen_calls = []
        self.mid = 100
        self.photos = [
            {"sha256": f"s{i}", "width": 4000, "height": 3000, "s3_key": f"orig/s{i}", "jpeg_1024_key": f"d/s{i}",
             "in_home_zone": i == 0, "place": {"neighborhood": "Home Hood", "city": "Homeville"} if i == 0 else {}, "tags": {}}
            for i in range(n_photos)
        ]
        mp.setattr(db, "get_collection", lambda id: self.collection)
        mp.setattr(db, "get_photos", lambda shas: [p for p in self.photos if p["sha256"] in shas])
        mp.setattr(db, "liked_examples", lambda limit=10: [])
        mp.setattr(db, "disliked_examples", lambda limit=10: [])
        mp.setattr(db, "set_collection_status", self.set_status)
        mp.setattr(db, "set_photo_states", lambda shas, st: (self.log.append("states:" + st), [self.photo_states.update({s: st}) for s in shas]))
        mp.setattr(db, "put_proposal", lambda p: self.proposals.__setitem__((p.id, p.version), p))
        mp.setattr(db, "get_proposal", self.get_proposal)
        mp.setattr(db, "update_proposal", self.update_proposal)
        mp.setattr(db, "list_proposals", lambda st: [p for p in self.proposals.values() if p.state == st])
        mp.setattr(db, "put_caption", self.put_caption)
        mp.setattr(db, "update_caption", self.update_caption)
        mp.setattr(db, "list_captions", lambda pid: [dict(c) for (p, _), c in sorted(self.captions.items()) if p == pid])
        mp.setattr(db, "put_feedback", lambda pid, kind, data: self.feedback.append((pid, kind, data)))
        mp.setattr(db, "list_feedback", lambda pid: [])
        mp.setattr(db, "put_message_ref", lambda m, pid, v, ci=None: self.refs.__setitem__(m, {"proposal_id": pid, "version": v, "caption_index": ci}))
        mp.setattr(db, "get_message_ref", lambda m: self.refs.get(m))
        mp.setattr(db, "put_learning_pending", lambda pid, s: self.pending.__setitem__(pid, s))
        mp.setattr(db, "get_learning_pending", lambda pid: self.pending.get(pid))
        mp.setattr(db, "clear_learning_pending", lambda pid: self.pending.pop(pid, None))
        mp.setattr(db, "approve_learning", lambda pid: (self.log.append("approve_learning"), 2)[1])
        mp.setattr(s3, "get_object_bytes", lambda k: k.encode())
        mp.setattr(renderer, "fit_crop", lambda box, ratio, w, h: [0, 0, 1, 1])
        mp.setattr(renderer, "render_slides", lambda pid, v, slides, originals: [replace(s, render_key=f"r/{pid}/{v}/{s.sha256}") for s in slides])
        mp.setattr(rules, "validate_caption", lambda t, h, c=3: list(self.violations))
        mp.setattr(curator_ai, "curate", lambda *a: Curation([p["sha256"] for p in reversed(self.photos)], {p["sha256"]: [0, 0, 1, 1] for p in self.photos}, {}, ["a #1 #2 #3", "b #1 #2 #3", "c #1 #2 #3"]))
        mp.setattr(curator_ai, "generate_captions", self.generate)
        mp.setattr(curator_ai, "interpret_message", lambda t, s, r: self.intent)
        mp.setattr(curator_ai, "summarize_learning", lambda pt, ev: {"post_type": pt, "summary": "r", "liked": [e.get("text", "") for e in ev], "disliked": []})
        mp.setattr(telegram, "send_message", self.send_message)
        mp.setattr(telegram, "send_media_group", self.send_media)
        mp.setattr(telegram, "edit_message", lambda m, t, rm=None: self.edits.append((m, t, rm)))
        mp.setattr(telegram, "answer_callback", lambda cid, text=None: self.answers.append(text))

    def set_status(self, id, status, expected=None):
        if expected is not None and self.status.get(id) != expected:
            return False
        self.status[id] = status
        self.log.append("status:" + status)
        return True

    def get_proposal(self, pid, version=None):
        vs = [v for (p, v) in self.proposals if p == pid]
        return self.proposals[(pid, version or max(vs))] if vs else None

    def update_proposal(self, p, expected_state):
        cur = self.proposals.get((p.id, p.version))
        if cur is None or cur.state != expected_state:
            return False
        self.proposals[(p.id, p.version)] = p
        return True

    def put_caption(self, pid, n, text, round_no, post_type, outcome="SHOWN"):
        self.captions[(pid, n)] = {"n": n, "text": text, "round": round_no, "outcome": outcome}

    def update_caption(self, pid, n, **attrs):
        self.captions[(pid, n)].update(attrs)

    def generate(self, ctx, history, instruction, liked, disliked, home, n=3):
        self.gen_calls.append((instruction, history, home))
        return [f"new{len(self.gen_calls)}-{i} #1 #2 #3" for i in range(n)]

    def send_message(self, text, reply_markup=None, reply_to=None):
        if self.fail_send:
            raise RuntimeError("boom")
        self.mid += 1
        self.sent.append((self.mid, text, reply_markup))
        self.log.append("msg")
        return self.mid

    def send_media(self, jpegs, caption=None):
        if self.fail_send:
            raise RuntimeError("boom")
        self.sent.append(("album", len(jpegs), caption))
        self.log.append("album")
        ids = list(range(self.mid + 1, self.mid + 1 + len(jpegs)))
        self.mid += len(jpegs)
        return ids

    def texts(self):
        return [s[1] for s in self.sent if isinstance(s[0], int)]

    def buttons(self, text_part):
        for m, t, kb in self.sent:
            if isinstance(m, int) and text_part in t and kb:
                return [b for row in kb["inline_keyboard"] for b in row]


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch)


def data(env, text):
    return {b["text"]: b["callback_data"] for b in env.buttons(text)}


def started(env):
    pid = flow.start_proposal("c1")
    return pid, env.proposals[(pid, 1)]


def test_start_creates_proposal_and_locks_after_sends(env):
    pid, p = started(env)
    assert p.state == "SENT" and p.post_type == "TRIP" and [s.sha256 for s in p.slides] == ["s2", "s1", "s0"]
    assert env.status["c1"] == "PROPOSED"
    assert env.log.index("album") < env.log.index("states:PROPOSED") and env.log[-1] == "states:PROPOSED"
    assert env.photo_states == {"s0": "PROPOSED", "s1": "PROPOSED", "s2": "PROPOSED"}
    assert [c["outcome"] for c in db.list_captions(pid)] == ["SHOWN"] * 3
    t = env.texts()
    assert t[:3] == ["Option 1: a #1 #2 #3", "Option 2: b #1 #2 #3", "Option 3: c #1 #2 #3"]
    use = data(env, "Option 2")
    assert use == {"Use this": f"use|{pid}|1|2"}
    assert set(data(env, "Pick")) == {"Edit caption", "Reorder/Remove", "Regenerate", "Reject"}
    assert [r["caption_index"] for m, r in sorted(env.refs.items()) if r["caption_index"]] == [1, 2, 3]
    assert datetime.fromisoformat(p.expires_at) - datetime.fromisoformat(p.created_at) == timedelta(days=config.PROPOSAL_EXPIRY_DAYS)


def test_start_not_ready_returns_none(env):
    env.status["c1"] = "SUPPRESSED"
    assert flow.start_proposal("c1") is None and env.sent == []


def test_start_failure_restores_ready(env):
    env.fail_send = True
    with pytest.raises(RuntimeError):
        flow.start_proposal("c1")
    assert env.status["c1"] == "READY" and env.photo_states == {}
    assert all(p.state == "SUPERSEDED" for p in env.proposals.values())


def test_callback_data_within_limit(env):
    pid, _ = started(env)
    assert all(len(b["callback_data"].encode()) <= 64 for _, _, kb in env.sent if isinstance(kb, dict) and "inline_keyboard" in kb for r in kb["inline_keyboard"] for b in r)


def test_use_idempotent_and_learning_summary(env):
    pid, _ = started(env)
    cbd = data(env, "Option 2")["Use this"]
    flow.handle_callback(cbd, "cb1", 555)
    p = env.proposals[(pid, 1)]
    assert p.state == "APPROVED" and p.chosen_caption == "b #1 #2 #3"
    assert env.captions[(pid, 2)]["outcome"] == "PICKED"
    assert env.edits[0][0] == 555 and env.edits[0][2] is None
    assert "Approved. Publishing arrives in the next phase." in env.texts()
    n = len(env.sent)
    flow.handle_callback(cbd, "cb2", 555)
    assert len(env.sent) == n and env.answers[-1] == flow.STALE
    assert pid in env.pending and set(data(env, "Learning summary")) == {"Add these", "Skip"}


def test_learning_buttons(env):
    pid, _ = started(env)
    flow.handle_callback(data(env, "Option 1")["Use this"], "x", 1)
    btn = data(env, "Learning summary")
    flow.handle_callback(btn["Add these"], "y", 1)
    assert "approve_learning" in env.log and pid not in env.pending and "Added 2 examples." in env.texts()
    flow.handle_callback(btn["Add these"], "z", 1)
    assert env.log.count("approve_learning") == 1


def test_learning_decline(env):
    pid, _ = started(env)
    flow.handle_callback(data(env, "Option 1")["Use this"], "x", 1)
    flow.handle_callback(data(env, "Learning summary")["Skip"], "y", 1)
    assert "approve_learning" not in env.log and pid not in env.pending


def test_stale_version_ignored(env):
    pid, _ = started(env)
    flow.handle_callback(f"use|{pid}|9|1", "cb", 1)
    assert env.proposals[(pid, 1)].state == "SENT" and env.answers[-1] == flow.STALE


def test_regen_button_new_round(env):
    pid, _ = started(env)
    flow.handle_callback(data(env, "Pick")["Regenerate"], "cb", None)
    assert env.gen_calls[0][0] is None and len(env.gen_calls[0][1]) == 3
    assert [env.captions[(pid, n)]["round"] for n in (4, 5, 6)] == [2, 2, 2]
    assert env.proposals[(pid, 1)].captions[0].startswith("new1")
    assert env.buttons("Option 1: new1")[0]["callback_data"] == f"use|{pid}|1|4"


def test_text_critique_regenerates_and_records(env):
    pid, _ = started(env)
    env.intent = Intent("generate_captions", {"instruction": "shorter"}, {"why": "too long"})
    flow.handle_text("shorter", None)
    assert env.feedback == [(pid, "critique", {"instruction": "shorter", "critique": {"why": "too long"}})]
    assert [env.captions[(pid, n)]["outcome"] for n in (1, 2, 3)] == ["CRITIQUED"] * 3
    assert env.captions[(pid, 1)]["critique"] == {"why": "too long"}
    assert env.gen_calls[0][0] == "shorter" and env.gen_calls[0][2] == ["Home Hood", "Homeville"] + config.HOME_PLACE_NAMES


def test_edit_via_reply(env):
    pid, _ = started(env)
    flow.handle_callback(data(env, "Pick")["Edit caption"], "cb", None)
    prompt = env.mid
    assert env.sent[-1][2] == {"force_reply": True} and env.refs[prompt]["caption_index"] == -1
    env.violations = ["need 3 hashtags"]
    flow.handle_text("bad", prompt)
    assert env.proposals[(pid, 1)].state == "SENT" and "need 3 hashtags" in env.texts()[-1]
    env.violations = []
    flow.handle_text("mine #1 #2 #3", prompt)
    p = env.proposals[(pid, 1)]
    assert p.state == "APPROVED" and p.chosen_caption == "mine #1 #2 #3"
    assert env.captions[(pid, 1)]["outcome"] == "EDITED" and env.captions[(pid, 1)]["final_text"] == "mine #1 #2 #3"


def test_text_select(env):
    pid, _ = started(env)
    env.intent = Intent("select", {"index": 3})
    flow.handle_text("the third", None)
    assert env.proposals[(pid, 1)].chosen_caption == "c #1 #2 #3"


def test_reorder_creates_v2(env):
    pid, _ = started(env)
    env.intent = Intent("reorder", {"order": [3, 1, 2]})
    flow.handle_text("3,1,2", None)
    v1, v2 = env.proposals[(pid, 1)], env.proposals[(pid, 2)]
    assert v1.state == "SUPERSEDED" and v2.state == "SENT" and v2.version == 2
    assert [s.sha256 for s in v2.slides] == ["s0", "s2", "s1"] and v2.slides[0].render_key == f"r/{pid}/2/s0"
    assert f"use|{pid}|2|1" in [b["callback_data"] for m, t, kb in env.sent if kb and "Option 1" in t for r in kb["inline_keyboard"] for b in r]
    flow.handle_callback(f"use|{pid}|1|1", "cb", 1)
    assert env.answers[-1] == flow.STALE


def test_reorder_invalid(env):
    pid, _ = started(env)
    env.intent = Intent("reorder", {"order": [1, 1, 2]})
    flow.handle_text("x", None)
    assert (pid, 2) not in env.proposals and "exactly once" in env.texts()[-1]


def test_remove_and_last_photo(env):
    pid, _ = started(env)
    env.intent = Intent("remove_photo", {"position": 1})
    flow.handle_text("remove 1", None)
    assert [s.sha256 for s in env.proposals[(pid, 2)].slides] == ["s1", "s0"]
    env.intent = Intent("remove_photo", {"position": 1})
    flow.handle_text("remove 1", None)
    assert [s.sha256 for s in env.proposals[(pid, 3)].slides] == ["s0"]
    flow.handle_text("remove 1", None)
    assert (pid, 4) not in env.proposals and "last photo" in env.texts()[-1]


def test_reject_via_buttons(env):
    pid, _ = started(env)
    flow.handle_callback(data(env, "Pick")["Reject"], "cb", None)
    reasons = data(env, "Why?")
    assert len(reasons) == 4 and env.proposals[(pid, 1)].state == "SENT"
    flow.handle_callback(reasons["Photos"], "cb", None)
    assert env.proposals[(pid, 1)].state == "REJECTED" and env.status["c1"] == "SUPPRESSED"
    assert set(env.photo_states.values()) == {"POOL"}
    assert {c["outcome"] for c in db.list_captions(pid)} == {"REJECTED"}
    assert pid in env.pending


def test_text_reject(env):
    pid, _ = started(env)
    env.intent = Intent("reject", {"reason": "boring"})
    flow.handle_text("nah", None)
    assert env.proposals[(pid, 1)].state == "REJECTED"


def test_chat(env):
    started(env)
    env.intent = Intent("chat", {"reply": "hello"})
    flow.handle_text("hey", None)
    assert env.texts()[-1] == "hello"


def test_text_without_proposal(env):
    flow.handle_text("hi", None)
    assert env.texts() == ["No open proposal."]


def test_expire_stale_no_dislike(env):
    pid, p = started(env)
    env.proposals[(pid, 1)] = replace(p, expires_at=(NOW - timedelta(days=1)).isoformat())
    fresh = replace(p, id="other", expires_at=(NOW + timedelta(days=1)).isoformat())
    env.proposals[("other", 1)] = fresh
    assert flow.expire_stale(NOW) == 1
    assert env.proposals[(pid, 1)].state == "EXPIRED" and env.proposals[("other", 1)].state == "SENT"
    assert env.status["c1"] == "SUPPRESSED" and set(env.photo_states.values()) == {"POOL"}
    assert {c["outcome"] for c in db.list_captions(pid)} == {"SHOWN"} and env.feedback == [] and pid not in env.pending
    assert "A proposal expired and its photos returned to the pool." in env.texts()
    assert flow.expire_stale(NOW) == 0


def test_album_chunk_numbering(monkeypatch):
    env = Env(monkeypatch, n_photos=14)
    flow.start_proposal("c1")
    albums = [s for s in env.sent if s[0] == "album"]
    assert [(a[1], a[2]) for a in albums] == [(10, "1-10 of 14"), (4, "11-14 of 14")]


def test_small_album_no_caption(env):
    started(env)
    assert [s for s in env.sent if s[0] == "album"] == [("album", 3, None)]


def test_deliver_alert(env):
    flow.deliver_alert("x")
    assert env.texts() == ["x"]
