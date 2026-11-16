import json
from functools import lru_cache

import boto3
from botocore.config import Config

from photo_sync import config, rules
from photo_sync.models import Curation, Intent

ACTIONS = ["generate_captions", "select", "edit_text", "reorder", "remove_photo", "reject", "chat"]
REJECT_REASONS = ["photos", "grouping", "captions", "boring", "other"]
CRITIQUE_DIMENSIONS = ["tone", "length", "emoji", "humor", "hashtags", "other"]

CAPTION_RULES = (
    "Caption rules (hard): English; exactly {tags} hashtags per caption; mention places at city or "
    "neighborhood level only; never include a street address; never mention any of these home "
    "places: {home}."
)

CURATE_SCHEMA = {
    "type": "object",
    "properties": {
        "order": {"type": "array", "items": {"type": "string"}},
        "crops": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        },
        "alt_text": {"type": "object", "additionalProperties": {"type": "string"}},
        "captions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["order", "crops", "alt_text", "captions"],
}
CAPTIONS_SCHEMA = {
    "type": "object",
    "properties": {"captions": {"type": "array", "items": {"type": "string"}}},
    "required": ["captions"],
}
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "params": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string"},
                "index": {"type": "integer"},
                "text": {"type": "string"},
                "order": {"type": "array", "items": {"type": "integer"}},
                "position": {"type": "integer"},
                "reason": {"type": "string", "enum": REJECT_REASONS},
                "reply": {"type": "string"},
            },
        },
        "critique": {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": CRITIQUE_DIMENSIONS},
                "direction": {"type": "string"},
            },
            "required": ["dimension", "direction"],
        },
    },
    "required": ["action", "params"],
}
LEARN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "liked": {"type": "array", "items": {"type": "string"}},
        "disliked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["text", "reason"],
            },
        },
    },
    "required": ["summary", "liked", "disliked"],
}


@lru_cache
def _client():
    return boto3.client(
        "bedrock-runtime", region_name=config.REGION, config=Config(retries={"max_attempts": 2}, read_timeout=300)
    )


def _call(model, system, content, tool, schema, max_tokens, temperature=0.3, messages=None):
    resp = _client().converse(
        modelId=model,
        system=[{"text": system}],
        messages=messages or [{"role": "user", "content": content}],
        toolConfig={
            "tools": [{"toolSpec": {"name": tool, "description": "Record the result.", "inputSchema": {"json": schema}}}],
            "toolChoice": {"tool": {"name": tool}},
        },
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    if resp.get("stopReason") == "max_tokens":
        raise ValueError("model output truncated")
    data = next(
        (c["toolUse"]["input"] for c in resp["output"]["message"]["content"] if c.get("toolUse", {}).get("name") == tool),
        None,
    )
    if not isinstance(data, dict):
        raise ValueError("model output missing tool call")
    return data, resp["output"]["message"]


def _examples(liked, disliked):
    lines = []
    if liked:
        lines.append("Captions the owner liked (match this style):")
        lines += [f"- [{e.get('post_type', '')}] {e['text']}" for e in liked]
    if disliked:
        lines.append("Captions the owner disliked (avoid this style):")
        for e in disliked:
            crit = f" (critique: {e['critique']})" if e.get("critique") else ""
            lines.append(f"- [{e.get('post_type', '')}] {e['text']}{crit}")
    return "\n".join(lines)


def _rules_text(home_names):
    return CAPTION_RULES.format(tags=config.HASHTAG_COUNT, home=", ".join(home_names) or "none")


def _bad_captions(captions, home_names, n=None):
    if not isinstance(captions, list) or not all(isinstance(c, str) for c in captions) or not captions:
        raise ValueError("captions malformed")
    if n is not None and len(captions) != n:
        raise ValueError(f"expected {n} captions, got {len(captions)}")
    out = []
    for c in captions:
        v = rules.validate_caption(c, home_names, config.HASHTAG_COUNT)
        if v:
            out.append((c, v))
    return out


def _violation_text(bad):
    return "These captions broke the rules:\n" + "\n".join(f"- {c!r}: {'; '.join(v)}" for c, v in bad) + (
        "\nReturn the complete result again with all captions fixed."
    )


def _with_retry(call, home_names, n):
    data, msg = call(None)
    bad = _bad_captions(data.get("captions"), home_names, n)
    if bad:
        data, _ = call((msg, _violation_text(bad)))
        if _bad_captions(data.get("captions"), home_names, n):
            raise ValueError("captions failed validation after retry")
    return data


def _retry_messages(first, retry):
    if not retry:
        return first
    msg, text = retry
    tool_use = next(c["toolUse"] for c in msg["content"] if "toolUse" in c)
    return first + [
        {"role": "assistant", "content": msg["content"]},
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": tool_use["toolUseId"],
                        "content": [{"text": text}],
                        "status": "error",
                    }
                }
            ],
        },
    ]


def curate(photos, post_type, place, liked, disliked, home_names):
    shas = [p["sha256"] for p in photos]
    system = (
        "You curate an Instagram carousel from a personal photo set. Order: slide 1 is the strongest, most "
        "eye-catching image because it sets the carousel aspect ratio; then tell a story (details -> people -> "
        "stronger image, or chronological when it fits); avoid near-identical frames; mix scenery, people and "
        "details; keep a pleasing color flow. For trips and events include all photos; for monthly dumps a subset "
        "is allowed. Crops are normalized [x0,y0,x1,y1] in 0..1 on the upright image, aimed at a 4:5 portrait "
        "crop that keeps the subject. Alt text is one plain sentence under 100 characters. Write exactly "
        f"{config.CAPTIONS_PER_ROUND} distinct caption options. " + _rules_text(home_names)
    )
    content = [
        {
            "text": f"Post type: {post_type}. Place: {place or 'unknown'}. {len(photos)} photos follow, each preceded "
            "by its label."
        }
    ]
    for p in photos:
        content.append(
            {
                "text": f"sha256={p['sha256']} size={p['width']}x{p['height']} captured={p.get('captured_at')} "
                f"place={p.get('place')} selfie={p.get('is_selfie', False)} tags={json.dumps(p.get('tags'))}"
            }
        )
        content.append({"image": {"format": "jpeg", "source": {"bytes": p["jpeg"]}}})
    ex = _examples(liked, disliked)
    if ex:
        content.append({"text": ex})
    first = [{"role": "user", "content": content}]

    def call(retry):
        return _call(
            config.CURATOR_MODEL, system, None, "record_curation", CURATE_SCHEMA, 2000,
            messages=_retry_messages(first, retry),
        )

    data = _with_retry(call, home_names, config.CAPTIONS_PER_ROUND)
    order = data.get("order")
    if not isinstance(order, list) or not order or len(set(order)) != len(order) or not set(order) <= set(shas):
        raise ValueError("invalid order")
    crops, alt = data.get("crops") or {}, data.get("alt_text") or {}
    out_crops = {}
    for s in order:
        c = crops.get(s)
        ok = isinstance(c, list) and len(c) == 4 and all(isinstance(v, (int, float)) for v in c)
        ok = ok and all(0 <= v <= 1 for v in c) and c[0] < c[2] and c[1] < c[3]
        out_crops[s] = [float(v) for v in c] if ok else [0.0, 0.0, 1.0, 1.0]
    if not all(isinstance(alt.get(s), str) and alt[s].strip() for s in order):
        raise ValueError("missing alt text")
    return Curation(order=order, crops=out_crops, alt_text={s: alt[s] for s in order}, captions=data["captions"])


def generate_captions(context, history, instruction, liked, disliked, home_names, n=3):
    system = (
        f"You write {n} distinct Instagram caption options for a personal photo post. They must differ from every "
        "caption already shown. " + _rules_text(home_names)
    )
    slides = "\n".join(
        f"{i}. scene={s.get('scene')} place={s.get('place')} tags={json.dumps(s.get('tags'))}"
        for i, s in enumerate(context.get("slides", []), 1)
    )
    parts = [
        f"Post type: {context.get('post_type')}. Place: {context.get('place') or 'unknown'}. "
        f"Captured: {context.get('captured_range')}.\nSlides:\n{slides}"
    ]
    ex = _examples(liked, disliked)
    if ex:
        parts.append(ex)
    if history:
        lines = ["Earlier rounds for this post (do not repeat these):"]
        for h in history:
            extra = "".join(
                f" [{k}: {h[k]}]" for k in ("outcome", "instruction", "critique") if h.get(k)
            )
            lines.append(f"- {h['text']}{extra}")
        parts.append("\n".join(lines))
    parts.append(
        f"Owner's latest request: {instruction}" if instruction else "Write a fresh round of options."
    )
    content = []
    if context.get("cover_jpeg"):
        content.append({"image": {"format": "jpeg", "source": {"bytes": context["cover_jpeg"]}}})
    content.append({"text": "\n\n".join(parts)})
    first = [{"role": "user", "content": content}]

    def call(retry):
        return _call(
            config.CAPTION_MODEL, system, None, "record_captions", CAPTIONS_SCHEMA, 1200,
            temperature=0.8, messages=_retry_messages(first, retry),
        )

    return _with_retry(call, home_names, n)["captions"]


def interpret_message(text, proposal_summary, replied_caption):
    caps = "\n".join(f"{i}. {c}" for i, c in enumerate(proposal_summary.get("captions", []), 1))
    system = (
        "You route the owner's Telegram reply about an Instagram post proposal to one action. generate_captions "
        "{instruction}: any request to change or redo captions (also include critique with dimension and a short "
        "direction); select {index} picks a 1-based caption number; edit_text {text} is a literal replacement "
        "caption; reorder {order} is a list of 1-based slide positions; remove_photo {position} is 1-based; "
        "reject {reason} is one of photos|grouping|captions|boring|other; chat {reply} is a short answer when no "
        "change is requested."
    )
    prompt = (
        f"Post type: {proposal_summary.get('post_type')}, {proposal_summary.get('slide_count')} slides.\n"
        f"Caption options:\n{caps}\n"
        f"Replied to caption: {replied_caption or 'none'}\nOwner message: {text}"
    )
    data, _ = _call(
        config.TAGGING_MODEL, system, [{"text": prompt}], "route_message", INTENT_SCHEMA, 500, temperature=0
    )
    action, params = data.get("action"), data.get("params")
    if action not in ACTIONS or not isinstance(params, dict):
        raise ValueError("invalid intent")
    required = {
        "generate_captions": ("instruction", str), "select": ("index", int), "edit_text": ("text", str),
        "reorder": ("order", list), "remove_photo": ("position", int), "reject": ("reason", str), "chat": ("reply", str),
    }[action]
    v = params.get(required[0])
    if not isinstance(v, required[1]) or isinstance(v, bool):
        raise ValueError(f"intent {action} missing {required[0]}")
    if action == "reorder" and not (v and all(isinstance(i, int) and not isinstance(i, bool) for i in v)):
        raise ValueError("invalid reorder")
    if action == "reject" and v not in REJECT_REASONS:
        raise ValueError("invalid reject reason")
    if action == "select" and v < 1:
        raise ValueError("invalid select index")
    crit = data.get("critique")
    if action == "generate_captions":
        if not (isinstance(crit, dict) and crit.get("dimension") in CRITIQUE_DIMENSIONS and isinstance(crit.get("direction"), str)):
            raise ValueError("invalid critique")
        crit = {"dimension": crit["dimension"], "direction": crit["direction"]}
    else:
        crit = None
    return Intent(action=action, params={required[0]: v}, critique=crit)


def summarize_learning(post_type, events):
    system = (
        "You summarize an owner's caption feedback for one post type. Ground every statement only in the events "
        "given; do not speculate beyond them. Summary is 2-4 sentences about the owner's taste for this post type. "
        "liked lists the texts of accepted captions (use final_text when the owner edited it); disliked lists the "
        "rejected captions with a short reason from the critique."
    )
    lines = []
    for e in events:
        extra = "".join(f" [{k}: {e[k]}]" for k in ("critique", "final_text") if e.get(k))
        lines.append(f"- {e['text']} => {e['outcome']}{extra}")
    data, _ = _call(
        config.CAPTION_MODEL, system, [{"text": f"Post type: {post_type}\nEvents:\n" + "\n".join(lines)}],
        "record_learning", LEARN_SCHEMA, 800, temperature=0.2,
    )
    summary, liked, disliked = data.get("summary"), data.get("liked"), data.get("disliked")
    if (
        not isinstance(summary, str) or not summary.strip()
        or not isinstance(liked, list) or not all(isinstance(t, str) for t in liked)
        or not isinstance(disliked, list)
        or not all(isinstance(d, dict) and isinstance(d.get("text"), str) and isinstance(d.get("reason"), str) for d in disliked)
    ):
        raise ValueError("invalid learning output")
    return {
        "post_type": post_type, "summary": summary, "liked": liked,
        "disliked": [{"text": d["text"], "reason": d["reason"]} for d in disliked],
    }
