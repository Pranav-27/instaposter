import base64
import hmac
import json
import logging

import pytest

from photo_sync import bot

BARE = {"statusCode": 200, "body": ""}
SECRET = "whsec"
CHAT = 42


@pytest.fixture
def env(monkeypatch):
    e = type("E", (), {})()
    e.invokes, e.flow, e.sent, e.seen = [], [], [], set()
    monkeypatch.setattr(bot.config, "telegram_secrets", lambda: {"bot_token": "TOKEN123", "chat_id": str(CHAT), "webhook_secret": SECRET})

    def seen_update(uid):
        if uid in e.seen:
            return True
        e.seen.add(uid)
        return False

    monkeypatch.setattr(bot.db, "seen_update", seen_update)
    monkeypatch.setattr(bot.flow, "handle_text", lambda *a: e.flow.append(("text", a)))
    monkeypatch.setattr(bot.flow, "handle_callback", lambda *a: e.flow.append(("cb", a)))
    monkeypatch.setattr(bot.flow, "deliver_alert", lambda t: e.flow.append(("alert", t)))
    monkeypatch.setattr(bot.telegram, "send_typing", lambda: e.sent.append("typing"))
    monkeypatch.setattr(bot.telegram, "send_message", lambda text, *a, **k: e.sent.append(text) or 1)

    class Client:
        def invoke(self, **kw):
            e.invokes.append(kw)

    monkeypatch.setattr(bot.boto3, "client", lambda *a, **k: Client())
    return e


def msg(text="hello", uid=CHAT, chat=CHAT, ctype="private", is_bot=False, update_id=1, **extra):
    return {"update_id": update_id, "message": {"message_id": 9, "from": {"id": uid, "is_bot": is_bot}, "chat": {"id": chat, "type": ctype}, "text": text, **extra}}


def cb(update_id=2, uid=CHAT, chat=CHAT):
    return {"update_id": update_id, "callback_query": {"id": "cbid", "from": {"id": uid, "is_bot": False}, "data": "a:1:2", "message": {"message_id": 5, "chat": {"id": chat, "type": "private"}}}}


def event(update, secret=SECRET, raw=None, b64=False):
    body = raw if raw is not None else json.dumps(update)
    headers = {} if secret is None else {"x-telegram-bot-api-secret-token": secret}
    if b64:
        body = base64.b64encode(body.encode()).decode()
    return {"requestContext": {}, "headers": headers, "body": body, "isBase64Encoded": b64}


def assert_dropped(env, ev):
    assert bot.handler(ev, None) == BARE
    assert env.invokes == [] and env.flow == [] and env.sent == []


def test_missing_secret(env):
    assert_dropped(env, event(msg(), secret=None))


def test_wrong_secret(env):
    assert_dropped(env, event(msg(), secret="nope"))


def test_wrong_sender(env):
    assert_dropped(env, event(msg(uid=7)))


def test_wrong_chat(env):
    assert_dropped(env, event(msg(chat=7)))


def test_group_chat(env):
    assert_dropped(env, event(msg(ctype="group")))


def test_channel_post(env):
    assert_dropped(env, event({"update_id": 3, "channel_post": msg()["message"]}))


def test_bot_sender(env):
    assert_dropped(env, event(msg(is_bot=True)))


def test_callback_wrong_chat(env):
    assert_dropped(env, event(cb(chat=7)))


def test_replay(env):
    ev = event(msg())
    bot.handler(ev, None)
    env.invokes.clear()
    assert_dropped(env, ev)


def test_oversized(env):
    assert_dropped(env, event(None, raw=json.dumps(msg(text="x" * 70000))))
    assert_dropped(env, event(None, raw=json.dumps(msg(text="x" * 70000)), b64=True))


def test_invalid_json(env):
    assert_dropped(env, event(None, raw="{nope"))


def test_unauthorized_does_not_consume_update_id(env):
    assert_dropped(env, event(msg(uid=7, update_id=11)))
    assert 11 not in env.seen


def test_valid_text(env):
    assert bot.handler(event(msg("make it warm")), None) == BARE
    assert len(env.invokes) == 1
    kw = env.invokes[0]
    assert kw["InvocationType"] == "Event" and kw["FunctionName"] == bot.config.BOT_FUNCTION
    assert json.loads(kw["Payload"]) == {"task": "text", "text": "make it warm", "reply_to": 9}
    assert env.flow == []


def test_base64_body(env):
    bot.handler(event(msg("b64"), b64=True), None)
    assert json.loads(env.invokes[0]["Payload"])["text"] == "b64"


def test_valid_callback(env):
    assert bot.handler(event(cb()), None) == BARE
    assert len(env.invokes) == 1
    assert json.loads(env.invokes[0]["Payload"]) == {"task": "callback", "data": "a:1:2", "callback_id": "cbid", "message_id": 5}


@pytest.mark.parametrize("text", ["/start", "/help", "/start@mybot"])
def test_start_direct(env, text):
    assert bot.handler(event(msg(text)), None) == BARE
    assert env.invokes == [] and env.flow == [] and env.sent == [bot.HELP]


def test_photo_ignored(env):
    m = msg()
    del m["message"]["text"]
    m["message"]["photo"] = [{}]
    assert_dropped(env, event(m))


def test_compare_digest_used(env, monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(bot.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    bot.handler(event(msg()), None)
    assert calls


def test_worker_text(env):
    bot.handler({"task": "text", "text": "hi", "reply_to": 4}, None)
    assert env.sent == ["typing"] and env.flow == [("text", ("hi", 4))]


def test_worker_callback(env):
    bot.handler({"task": "callback", "data": "d", "callback_id": "c", "message_id": 5}, None)
    assert env.flow == [("cb", ("d", "c", 5))]


def test_worker_failure(env, monkeypatch, caplog):
    def boom(*a):
        raise ValueError("private text TOKEN123")

    monkeypatch.setattr(bot.flow, "handle_text", boom)
    with caplog.at_level(logging.DEBUG):
        assert bot.handler({"task": "text", "text": "secret words", "reply_to": None}, None) == BARE
    assert env.sent[-1] == "Something went wrong, please try again"
    assert "TOKEN123" not in caplog.text and "secret words" not in caplog.text and "ValueError" in caplog.text


def test_worker_failure_notice_fails(env, monkeypatch):
    monkeypatch.setattr(bot.flow, "handle_text", lambda *a: 1 / 0)
    monkeypatch.setattr(bot.telegram, "send_message", lambda *a, **k: 1 / 0)
    assert bot.handler({"task": "text", "text": "x", "reply_to": None}, None) == BARE


def test_sns(env):
    ev = {"Records": [{"Sns": {"Subject": " Alarm ", "Message": " " + "m" * 5000}}]}
    assert bot.handler(ev, None) == BARE
    (kind, text), = env.flow
    assert kind == "alert" and text.startswith("AWS alert: Alarm") and len(text) <= 3500


def test_unknown_event(env):
    assert bot.handler({"foo": 1}, None) == BARE
    assert bot.handler({"Records": []}, None) == BARE
    assert env.flow == [] and env.invokes == []
