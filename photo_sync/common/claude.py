import json
from functools import lru_cache

import boto3
from botocore.config import Config

from photo_sync import config
from photo_sync.models import Tags

TOOL = "record_tags"

SYSTEM = (
    "You tag a personal photo for Instagram curation. Be concise and accurate. "
    "Flag screenshots, documents and receipts via is_screenshot, is_document, is_receipt "
    "(and set is_photo false for them). quality and blur_score are 0 to 1 "
    "(blur_score 1 = very blurry). tags are short lowercase keywords."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {"type": "array", "items": {"type": "string"}},
        "scene": {"type": "string"},
        "mood": {"type": "string"},
        "quality": {"type": "number"},
        "people": {"type": "integer"},
        "food": {"type": "boolean"},
        "landmark": {"type": ["string", "null"]},
        "is_photo": {"type": "boolean"},
        "is_screenshot": {"type": "boolean"},
        "is_document": {"type": "boolean"},
        "is_receipt": {"type": "boolean"},
        "faces_visible": {"type": "boolean"},
        "eyes_closed": {"type": "boolean"},
        "blur_score": {"type": "number"},
        "text_visible": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": [k for k in Tags.__dataclass_fields__ if k != "landmark"],
}


@lru_cache
def _client():
    return boto3.client(
        "bedrock-runtime", region_name=config.REGION, config=Config(retries={"max_attempts": 2})
    )


def tag_photo(jpeg_1024: bytes) -> Tags:
    resp = _client().converse(
        modelId=config.TAGGING_MODEL,
        system=[{"text": SYSTEM}],
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": jpeg_1024}}},
                    {"text": "Tag this photo."},
                ],
            }
        ],
        toolConfig={
            "tools": [
                {"toolSpec": {"name": TOOL, "description": "Record photo tags.", "inputSchema": {"json": SCHEMA}}}
            ],
            "toolChoice": {"tool": {"name": TOOL}},
        },
        inferenceConfig={"maxTokens": 600, "temperature": 0},
    )
    if resp.get("stopReason") == "max_tokens":
        raise ValueError("tagging output truncated")
    data = next(
        (
            c["toolUse"]["input"]
            for c in resp["output"]["message"]["content"]
            if c.get("toolUse", {}).get("name") == TOOL
        ),
        None,
    )
    if not isinstance(data, dict):
        raise ValueError("tagging output missing tool call")
    fields = Tags.__dataclass_fields__
    missing = [k for k in SCHEMA["required"] if k not in data]
    if missing:
        raise ValueError(f"tagging output missing fields: {missing}")
    t = Tags(**{k: data[k] for k in fields if k in data})
    if not isinstance(t.tags, list) or not all(isinstance(s, str) for s in t.tags):
        raise ValueError("tagging output has malformed tags")
    t.quality, t.blur_score, t.people = float(t.quality), float(t.blur_score), int(t.people)
    return t
