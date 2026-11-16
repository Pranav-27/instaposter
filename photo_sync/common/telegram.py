import json
import time
import urllib.error
import urllib.request
import uuid

from photo_sync import config

TIMEOUT = 15
MEDIA_TIMEOUT = 60
MAX_RETRY_AFTER = 5
GROUP_SIZE = 10


def _call(method: str, body: bytes, content_type: str, timeout: int = TIMEOUT) -> dict:
    token = config.telegram_secrets()["bot_token"]
    for attempt in (0, 1):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=body,
            headers={"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
        except Exception as e:
            raise RuntimeError(f"telegram {method} failed: {type(e).__name__}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise RuntimeError(f"telegram {method} failed: invalid response") from None
        if not isinstance(data, dict):
            raise RuntimeError(f"telegram {method} failed: invalid response")
        if data.get("ok"):
            return data
        params = data.get("parameters") or {}
        if data.get("error_code") == 429 and attempt == 0:
            time.sleep(min(max(float(params.get("retry_after", 1)), 0), MAX_RETRY_AFTER))
            continue
        raise RuntimeError(f"telegram {method} failed: {data.get('error_code')} {str(data.get('description', ''))[:100]}")
    raise RuntimeError(f"telegram {method} failed")


def _json(method: str, payload: dict) -> dict:
    payload = {"chat_id": int(config.telegram_secrets()["chat_id"]), **payload}
    return _call(method, json.dumps(payload).encode(), "application/json")


def send_message(text: str, reply_markup: dict | None = None, reply_to: int | None = None) -> int:
    payload = {"text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if reply_to is not None:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return _json("sendMessage", payload)["result"]["message_id"]


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, data in files.items():
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_media_group(jpegs: list[bytes], caption: str | None = None) -> list[int]:
    chat_id = config.telegram_secrets()["chat_id"]
    ids = []
    for start in range(0, len(jpegs), GROUP_SIZE):
        chunk = jpegs[start : start + GROUP_SIZE]
        media = []
        files = {}
        for i, data in enumerate(chunk):
            item = {"type": "photo", "media": f"attach://photo{i}"}
            if start == 0 and i == 0 and caption:
                item["caption"] = caption
            media.append(item)
            files[f"photo{i}"] = data
        body, ctype = _multipart({"chat_id": chat_id, "media": json.dumps(media)}, files)
        result = _call("sendMediaGroup", body, ctype, MEDIA_TIMEOUT)["result"]
        ids.extend(m["message_id"] for m in result)
    return ids


def edit_message(message_id: int, text: str, reply_markup: dict | None = None) -> None:
    payload = {"message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _json("editMessageText", payload)


def answer_callback(callback_id: str, text: str | None = None) -> None:
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    _call("answerCallbackQuery", json.dumps(payload).encode(), "application/json")


def send_typing() -> None:
    _json("sendChatAction", {"action": "typing"})
