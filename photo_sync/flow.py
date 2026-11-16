import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from photo_sync import config, renderer, rules
from photo_sync.common import curator_ai, db, s3, telegram
from photo_sync.models import Proposal, Slide

REASONS = {"photos": "Photos", "grouping": "Grouping", "captions": "Captions", "boring": "Not interesting"}
LEARN_OUTCOMES = {"PICKED", "EDITED", "CRITIQUED", "REJECTED"}
STALE = "This proposal changed"


def cb(action: str, proposal_id: str, version: int, arg: str = "") -> str:
    return f"{action}|{proposal_id}|{version}|{arg}"


def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def _shas(p: Proposal) -> list[str]:
    return [s.sha256 for s in p.slides]


def _home_names(photos: list[dict]) -> list[str]:
    names = []
    for ph in photos:
        if ph.get("in_home_zone") is True:
            place = ph.get("place") or {}
            names += [n for n in (place.get("neighborhood"), place.get("city")) if n]
    return list(dict.fromkeys(names + config.HOME_PLACE_NAMES))


def _when(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime("%A %Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _place_name(ph: dict) -> str | None:
    place = ph.get("place") or {}
    return place.get("neighborhood") or place.get("city")


def _examples() -> tuple[list[dict], list[dict]]:
    return db.liked_examples(config.CAPTION_EXAMPLES), db.disliked_examples(config.CAPTION_EXAMPLES)


def _context(p: Proposal, photos: list[dict]) -> dict:
    by_sha = {ph["sha256"]: ph for ph in photos}
    ordered = [by_sha[s.sha256] for s in p.slides if s.sha256 in by_sha]
    times = sorted(ph["captured_at"] for ph in ordered if ph.get("captured_at"))
    return {
        "post_type": p.post_type,
        "place": p.place,
        "captured_range": f"{_when(times[0])} to {_when(times[-1])}" if times else None,
        "slides": [
            {
                "tags": (ph.get("tags") or {}).get("tags"),
                "scene": (ph.get("tags") or {}).get("scene"),
                "place": (ph.get("place") or {}).get("neighborhood") or (ph.get("place") or {}).get("city"),
            }
            for ph in ordered
        ],
    }


def _send_album(p: Proposal, slides: list[Slide]) -> None:
    jpegs = [s3.get_object_bytes(s.render_key) for s in slides]
    total = len(jpegs)
    for i in range(0, total, 10):
        chunk = jpegs[i : i + 10]
        caption = f"{i + 1}-{i + len(chunk)} of {total}" if total > 10 else None
        for mid in telegram.send_media_group(chunk, caption):
            db.put_message_ref(mid, p.id, p.version)


def _send_options(p: Proposal, options: list[tuple[int, str]]) -> None:
    for pos, (n, text) in enumerate(options, 1):
        mid = telegram.send_message(f"Option {pos}: {text}", _kb([[("Use this", cb("use", p.id, p.version, str(n)))]]))
        db.put_message_ref(mid, p.id, p.version, n)


def _send_controls(p: Proposal) -> None:
    kb = _kb(
        [
            [("Edit caption", cb("edit", p.id, p.version)), ("Reorder/Remove", cb("rm", p.id, p.version))],
            [("Regenerate", cb("regen", p.id, p.version)), ("Reject", cb("rj", p.id, p.version))],
        ]
    )
    mid = telegram.send_message("Pick a caption, or reply with feedback.", kb)
    db.put_message_ref(mid, p.id, p.version)


def _last_round(captions: list[dict]) -> list[dict]:
    if not captions:
        return []
    top = max(c.get("round", 1) for c in captions)
    return sorted((c for c in captions if c.get("round", 1) == top), key=lambda c: c["n"])


def _resend_round(p: Proposal) -> None:
    _send_options(p, [(c["n"], c["text"]) for c in _last_round(db.list_captions(p.id))])
    _send_controls(p)


def start_proposal(collection_id: str) -> str | None:
    if not db.set_collection_status(collection_id, "PROPOSED", expected="READY"):
        return None
    proposal = None
    try:
        item = db.get_collection(collection_id)
        photos = db.get_photos(item["photo_shas"])
        by_sha = {ph["sha256"]: ph for ph in photos}
        liked, disliked = _examples()
        home_names = _home_names(photos)
        inputs = [
            {**ph, "captured_at": _when(ph.get("captured_at")), "place": _place_name(ph), "jpeg": s3.get_object_bytes(ph["jpeg_1024_key"])} for ph in photos
        ]
        cur = curator_ai.curate(inputs, item["type"], item.get("place"), liked, disliked, home_names)
        slides = [
            Slide(
                sha,
                renderer.fit_crop(cur.crops[sha], config.CAROUSEL_RATIO, by_sha[sha]["width"], by_sha[sha]["height"]),
                cur.alt_text.get(sha, ""),
            )
            for sha in cur.order
        ]
        now = datetime.now(timezone.utc)
        proposal = Proposal(
            id=uuid.uuid4().hex[:12],
            collection_id=collection_id,
            version=1,
            state="SENT",
            post_type=item["type"],
            slides=slides,
            captions=list(cur.captions),
            chosen_caption=None,
            place=item.get("place"),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=config.PROPOSAL_EXPIRY_DAYS)).isoformat(),
        )
        proposal.slides = renderer.render_slides(proposal.id, 1, slides, {s.sha256: by_sha[s.sha256]["s3_key"] for s in slides})
        db.put_proposal(proposal)
        for n, text in enumerate(proposal.captions, 1):
            db.put_caption(proposal.id, n, text, 1, proposal.post_type)
        _send_album(proposal, proposal.slides)
        _send_options(proposal, list(enumerate(proposal.captions, 1)))
        _send_controls(proposal)
        db.set_photo_states(_shas(proposal), "PROPOSED")
    except Exception:
        if proposal is not None:
            db.update_proposal(replace(proposal, state="SUPERSEDED"), "SENT")
        db.set_collection_status(collection_id, "READY")
        raise
    return proposal.id


def _current(proposal_id: str, version: int | None = None) -> Proposal | None:
    p = db.get_proposal(proposal_id)
    if p is None or p.state != "SENT" or (version is not None and p.version != version):
        return None
    return p


def _learning_summary(p: Proposal) -> None:
    events = [c for c in db.list_captions(p.id) if c.get("outcome") in LEARN_OUTCOMES]
    if not events:
        return
    summary = curator_ai.summarize_learning(p.post_type, events)
    db.put_learning_pending(p.id, summary)
    lines = [f"Learning summary ({p.post_type.replace('_', ' ').lower()})"]
    if summary.get("summary"):
        lines.append(f"Claude's reading: {summary['summary']}")
    lines += [f"- liked: {text}" for text in summary.get("liked", [])]
    lines += [f"- disliked: {d.get('text', '')} ({d.get('reason', '')})" for d in summary.get("disliked", [])]
    telegram.send_message(
        "\n".join(lines), _kb([[("Add these", cb("ly", p.id, p.version)), ("Skip", cb("ln", p.id, p.version))]])
    )


def _approve(p: Proposal, n: int | None, text: str, outcome: str, message_id: int | None = None) -> None:
    if not db.update_proposal(replace(p, state="APPROVED", chosen_caption=text), "SENT"):
        return
    if n is not None:
        attrs = {"final_text": text} if outcome == "EDITED" else {}
        db.update_caption(p.id, n, outcome=outcome, **attrs)
    if message_id is not None:
        telegram.edit_message(message_id, f"Chosen: {text}", None)
    telegram.send_message("Approved. Publishing arrives in the next phase.")
    _learning_summary(p)


def _caption_text(p: Proposal, n: int) -> tuple[int, str] | None:
    for c in db.list_captions(p.id):
        if c["n"] == n:
            return n, c["text"]
    return None


def _regenerate(p: Proposal, instruction: str | None = None, critique: dict | None = None) -> None:
    caps = db.list_captions(p.id)
    last = _last_round(caps)
    if critique is not None:
        db.put_feedback(p.id, "critique", {"instruction": instruction, "critique": critique})
        for c in last:
            if c.get("outcome", "SHOWN") == "SHOWN":
                db.update_caption(p.id, c["n"], outcome="CRITIQUED", critique=critique)
    photos = db.get_photos(_shas(p))
    liked, disliked = _examples()
    texts = curator_ai.generate_captions(
        _context(p, photos), caps, instruction, liked, disliked, _home_names(photos), config.CAPTIONS_PER_ROUND
    )
    round_no = (last[0].get("round", 1) if last else 0) + 1
    base = max((c["n"] for c in caps), default=0)
    for i, text in enumerate(texts, 1):
        db.put_caption(p.id, base + i, text, round_no, p.post_type)
    if not db.update_proposal(replace(p, captions=list(texts)), "SENT"):
        return
    _send_options(p, [(base + i, t) for i, t in enumerate(texts, 1)])


def _new_version(p: Proposal, slides: list[Slide]) -> None:
    photos = db.get_photos([s.sha256 for s in slides])
    by_sha = {ph["sha256"]: ph for ph in photos}
    version = p.version + 1
    rendered = renderer.render_slides(p.id, version, slides, {s.sha256: by_sha[s.sha256]["s3_key"] for s in slides})
    new = replace(p, version=version, slides=rendered, chosen_caption=None)
    db.put_proposal(new)
    if not db.update_proposal(replace(p, state="SUPERSEDED"), "SENT"):
        db.update_proposal(replace(new, state="SUPERSEDED"), "SENT")
        return
    _send_album(new, rendered)
    _resend_round(new)


def _reorder(p: Proposal, order: list) -> None:
    try:
        order = [int(i) for i in order]
    except (TypeError, ValueError):
        order = []
    if sorted(order) != list(range(1, len(p.slides) + 1)):
        telegram.send_message(f"Give every slide number from 1 to {len(p.slides)} exactly once, e.g. 3,1,2.")
        return
    _new_version(p, [p.slides[i - 1] for i in order])


def _remove(p: Proposal, index) -> None:
    try:
        i = int(index)
    except (TypeError, ValueError):
        i = 0
    if not 1 <= i <= len(p.slides):
        telegram.send_message(f"Pick a slide number from 1 to {len(p.slides)}.")
        return
    if len(p.slides) == 1:
        telegram.send_message("Cannot remove the last photo.")
        return
    _new_version(p, [s for k, s in enumerate(p.slides, 1) if k != i])


def _reject(p: Proposal, reason: str) -> None:
    if not db.update_proposal(replace(p, state="REJECTED"), "SENT"):
        return
    db.set_photo_states(_shas(p), "POOL")
    db.set_collection_status(p.collection_id, "SUPPRESSED")
    for c in db.list_captions(p.id):
        db.update_caption(p.id, c["n"], outcome="REJECTED")
    db.put_feedback(p.id, "reject", {"reason": reason})
    telegram.send_message("Rejected. Photos returned to the pool.")
    _learning_summary(p)


def _edit_caption(p: Proposal, text: str, n: int | None) -> None:
    photos = db.get_photos(_shas(p))
    violations = rules.validate_caption(text, _home_names(photos), config.HASHTAG_COUNT)
    if violations:
        telegram.send_message("Fix before I can use it: " + "; ".join(violations))
        return
    if n is None:
        last = _last_round(db.list_captions(p.id))
        n = last[0]["n"] if last else None
    _approve(p, n, text, "EDITED")


def handle_callback(data: str, callback_id: str, message_id: int | None) -> None:
    parts = data.split("|", 3)
    if len(parts) != 4:
        telegram.answer_callback(callback_id)
        return
    action, pid, version, arg = parts
    try:
        version = int(version)
    except ValueError:
        telegram.answer_callback(callback_id)
        return
    if action in ("ly", "ln"):
        telegram.answer_callback(callback_id)
        if db.get_learning_pending(pid) is None:
            return
        if action == "ly":
            n = db.approve_learning(pid)
            telegram.send_message(f"Added {n} examples.")
        else:
            telegram.send_message("Skipped. Nothing added.")
        db.clear_learning_pending(pid)
        return
    p = _current(pid, version)
    if p is None:
        telegram.answer_callback(callback_id, STALE)
        return
    telegram.answer_callback(callback_id)
    if action == "use":
        found = _caption_text(p, int(arg)) if arg.isdigit() else None
        if found:
            _approve(p, found[0], found[1], "PICKED", message_id)
    elif action == "edit":
        mid = telegram.send_message("Reply to this message with your caption.", {"force_reply": True})
        db.put_message_ref(mid, p.id, p.version, -1)
    elif action == "regen":
        _regenerate(p)
    elif action == "rm":
        telegram.send_message("Reply with a new order like 3,1,2 or say remove 4")
    elif action == "rj":
        if arg in REASONS:
            _reject(p, arg)
        else:
            telegram.send_message(
                "Why?", _kb([[(label, cb("rj", p.id, p.version, key))] for key, label in REASONS.items()])
            )


def handle_text(text: str, reply_to_message_id: int | None) -> None:
    ref = db.get_message_ref(reply_to_message_id) if reply_to_message_id else None
    p = _current(ref["proposal_id"]) if ref else None
    if p is None:
        sent = sorted(db.list_proposals("SENT"), key=lambda x: x.created_at)
        p = sent[-1] if sent else None
    if p is None:
        telegram.send_message("No open proposal.")
        return
    index = ref.get("caption_index") if ref and ref["proposal_id"] == p.id else None
    if index == -1:
        _edit_caption(p, text, None)
        return
    replied = _caption_text(p, index) if index and index > 0 else None
    summary = {"captions": p.captions, "slide_count": len(p.slides), "post_type": p.post_type}
    intent = curator_ai.interpret_message(text, summary, replied[1] if replied else None)
    params = intent.params or {}
    if intent.action == "generate_captions":
        _regenerate(p, params.get("instruction") or text, intent.critique or {"instruction": text})
    elif intent.action == "select":
        n = params.get("index")
        last = _last_round(db.list_captions(p.id))
        if isinstance(n, int) and 1 <= n <= len(last):
            _approve(p, last[n - 1]["n"], last[n - 1]["text"], "PICKED")
        else:
            telegram.send_message("Which option?")
    elif intent.action == "edit_text":
        _edit_caption(p, params.get("text", ""), index if index and index > 0 else None)
    elif intent.action == "reorder":
        _reorder(p, params.get("order") or [])
    elif intent.action == "remove_photo":
        _remove(p, params.get("position"))
    elif intent.action == "reject":
        _reject(p, params.get("reason", "boring"))
    else:
        telegram.send_message(params.get("reply", ""))


def expire_stale(now: datetime) -> int:
    count = 0
    for p in db.list_proposals("SENT"):
        expires = datetime.fromisoformat(p.expires_at)
        if expires.tzinfo is None and now.tzinfo is not None:
            expires = expires.replace(tzinfo=now.tzinfo)
        if expires >= now:
            continue
        if not db.update_proposal(replace(p, state="EXPIRED"), "SENT"):
            continue
        db.set_photo_states(_shas(p), "POOL")
        db.set_collection_status(p.collection_id, "SUPPRESSED")
        telegram.send_message("A proposal expired and its photos returned to the pool.")
        count += 1
    return count


def deliver_alert(text: str) -> None:
    telegram.send_message(text)
