import base64
import hmac
import json
import logging

import boto3

from photo_sync import config, flow
from photo_sync.common import db, telegram

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
ALERT_MAX = 3500
DROP = {"statusCode": 200, "body": ""}
HELP = "Photo pipeline bot. Send a message to give feedback or a caption; use the buttons on proposals to approve."
ERROR_TEXT = "Something went wrong, please try again"


def _parse(event: dict, secrets: dict) -> dict | None:
    body = event.get("body") or ""
    if len(body) > MAX_BODY * 2:
        return None
    try:
        raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    except Exception:
        return None
    if len(raw) > MAX_BODY:
        return None
    header = (event.get("headers") or {}).get("x-telegram-bot-api-secret-token")
    if not isinstance(header, str) or not hmac.compare_digest(
        header.encode(), str(secrets["webhook_secret"]).encode()
    ):
        return None
    try:
        update = json.loads(raw)
    except ValueError:
        return None
    return update if isinstance(update, dict) else None


def _authorized(update: dict, chat_id: int) -> tuple[str, dict, dict] | None:
    kind = "message" if "message" in update else "callback_query" if "callback_query" in update else None
    if kind is None:
        return None
    obj = update[kind]
    if not isinstance(obj, dict):
        return None
    sender = obj.get("from")
    message = obj if kind == "message" else obj.get("message")
    if not isinstance(sender, dict) or not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return None
    if sender.get("is_bot") or sender.get("id") != chat_id or chat.get("id") != chat_id:
        return None
    return kind, obj, message


def _enqueue(payload: dict) -> None:
    boto3.client("lambda", region_name=config.REGION).invoke(
        FunctionName=config.BOT_FUNCTION, InvocationType="Event", Payload=json.dumps(payload).encode()
    )


def _webhook(event: dict) -> dict:
    secrets = config.telegram_secrets()
    update = _parse(event, secrets)
    if update is None:
        return DROP
    checked = _authorized(update, int(secrets["chat_id"]))
    if checked is None:
        return DROP
    kind, obj, message = checked
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or db.seen_update(update_id):
        return DROP
    if kind == "message":
        text = obj.get("text")
        if not isinstance(text, str) or not text:
            return DROP
        if text.split()[0].split("@")[0] in ("/start", "/help"):
            telegram.send_message(HELP)
        else:
            _enqueue({"task": "text", "text": text, "reply_to": obj.get("message_id")})
    else:
        data = obj.get("data")
        if not isinstance(data, str) or not obj.get("id"):
            return DROP
        _enqueue({"task": "callback", "data": data, "callback_id": obj["id"], "message_id": message.get("message_id")})
    return DROP


def _worker(event: dict) -> None:
    try:
        telegram.send_typing()
        if event["task"] == "text":
            flow.handle_text(event["text"], event.get("reply_to"))
        elif event["task"] == "callback":
            flow.handle_callback(event["data"], event["callback_id"], event.get("message_id"))
    except Exception as e:
        log.error("worker failed: %s", type(e).__name__)
        try:
            telegram.send_message(ERROR_TEXT)
        except Exception as e2:
            log.error("error notice failed: %s", type(e2).__name__)


def _sns(event: dict) -> None:
    sns = event["Records"][0]["Sns"]
    text = f"AWS alert: {(sns.get('Subject') or '').strip()}\n{(sns.get('Message') or '').strip()}".strip()
    flow.deliver_alert(text[:ALERT_MAX])


def _is_sns(event: dict) -> bool:
    records = event.get("Records")
    return isinstance(records, list) and bool(records) and isinstance(records[0], dict) and "Sns" in records[0]


def handler(event, context):
    if not isinstance(event, dict):
        return dict(DROP)
    if "requestContext" in event and "headers" in event:
        return dict(_webhook(event))
    if "task" in event:
        _worker(event)
    elif _is_sns(event):
        _sns(event)
    return dict(DROP)
