import io
import json
import urllib.error

import pytest

from photo_sync.common import telegram


@pytest.fixture
def api(monkeypatch):
    calls = []
    responses = []
    monkeypatch.setattr(telegram.config, "telegram_secrets", lambda: {"bot_token": "SECRETTOKEN", "chat_id": "42", "webhook_secret": "w"})
    monkeypatch.setattr(telegram.time, "sleep", lambda s: calls.append(("sleep", s)))

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        calls.append((req.full_url.rsplit("/", 1)[1], req.data, req.get_header("Content-type"), timeout))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return Resp(json.dumps(r).encode())

    monkeypatch.setattr(telegram.urllib.request, "urlopen", urlopen)
    return calls, responses


def test_send_message(api):
    calls, responses = api
    responses.append({"ok": True, "result": {"message_id": 7}})
    assert telegram.send_message("hi", {"k": 1}, reply_to=3) == 7
    method, body, ctype, timeout = calls[0]
    assert method == "sendMessage" and timeout
    payload = json.loads(body)
    assert payload["chat_id"] == 42 and payload["text"] == "hi" and payload["reply_markup"] == {"k": 1}
    assert payload["reply_parameters"]["message_id"] == 3


def test_edit_answer_typing(api):
    calls, responses = api
    responses.extend([{"ok": True, "result": {}}] * 3)
    telegram.edit_message(5, "t")
    telegram.answer_callback("cb", "done")
    telegram.send_typing()
    assert [c[0] for c in calls] == ["editMessageText", "answerCallbackQuery", "sendChatAction"]
    assert json.loads(calls[1][1]) == {"callback_query_id": "cb", "text": "done"}
    assert json.loads(calls[2][1])["action"] == "typing"


def test_media_group_chunks(api):
    calls, responses = api
    responses.append({"ok": True, "result": [{"message_id": i} for i in range(10)]})
    responses.append({"ok": True, "result": [{"message_id": 10}, {"message_id": 11}]})
    jpegs = [b"J%02d" % i for i in range(12)]
    ids = telegram.send_media_group(jpegs, caption="cap")
    assert ids == list(range(12))
    assert len(calls) == 2
    first, second = calls[0][1], calls[1][1]
    assert calls[0][2].startswith("multipart/form-data; boundary=")
    assert first.count(b"attach://photo") == 10 and second.count(b"attach://photo") == 2
    assert first.count(b"\"caption\"") == 1 and b"caption" not in second
    assert b"J00" in first and b"J09" in first and b"J10" in second and b"J10" not in first
    assert b'"caption": "cap"' in first


def test_retry_429_capped(api):
    calls, responses = api
    responses.append({"ok": False, "error_code": 429, "parameters": {"retry_after": 999}})
    responses.append({"ok": True, "result": {"message_id": 1}})
    assert telegram.send_message("x") == 1
    assert ("sleep", telegram.MAX_RETRY_AFTER) in calls


def test_429_twice_raises(api):
    _, responses = api
    responses.extend([{"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}] * 2)
    with pytest.raises(RuntimeError):
        telegram.send_message("x")


def test_error_has_no_token(api):
    _, responses = api
    responses.append({"ok": False, "error_code": 400, "description": "bad"})
    with pytest.raises(RuntimeError) as ei:
        telegram.send_message("x")
    assert "SECRETTOKEN" not in str(ei.value)


def test_network_and_http_errors_have_no_token(api):
    _, responses = api
    url = "https://api.telegram.org/botSECRETTOKEN/sendMessage"
    responses.append(urllib.error.URLError("boom " + url))
    with pytest.raises(RuntimeError) as ei:
        telegram.send_message("x")
    assert "SECRETTOKEN" not in str(ei.value) and ei.value.__cause__ is None
    responses.append(urllib.error.HTTPError(url, 500, "err", {}, io.BytesIO(b"not json")))
    with pytest.raises(RuntimeError) as ei:
        telegram.send_message("x")
    assert "SECRETTOKEN" not in str(ei.value)
