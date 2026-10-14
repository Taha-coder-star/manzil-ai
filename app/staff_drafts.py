"""Staff extraction, spans-only contract and span verification (Task 8.2 slice 1;
docs/STAFF_EXTRACTION.md sections 2 and 7; Red-Team R-08, R-22, R-37).

The model of a staff turn only chooses a tool and copies raw spans from the staff
member's own messages. It never supplies normalised values, counts, property IDs,
photo references or an agency / staff identity: the tool schemas (app.ai_tools)
refuse those, and the functions here verify that every span really occurs in the
messages the model saw. Normalisation, conflicts, duplicates and persistence come
in later slices; nothing here reads or writes the database.

A span is verified as in R-22: a whitespace- and case-insensitive substring of ONE
message (it never straddles two). Never log spans or message text.
"""

import re
from dataclasses import dataclass

DRAFT_FIELDS = ("purpose", "property_type", "location", "size", "price", "bedrooms",
                "bathrooms", "construction", "source", "parking", "description")
FIND_FIELDS = ("code", "location", "property_type", "purpose", "size", "price", "bedrooms")
# Two drafts of one message may share a word, not a listing (R-08).
OVERLAP_LIMIT = 0.20
CODE = re.compile(r"#?([A-Za-z0-9]{2,4}-[0-9]{3})")
_CODE_IN_TEXT = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{2,4}-[0-9]{3})(?![0-9])")


@dataclass(frozen=True)
class StaffContext:
    """What a staff turn's tools may verify spans against, aligned with the engine's
    "Current messages" numbering: texts[n - 1] is message n ('' when it has no text,
    e.g. a photo), descriptions are the 1-based numbers of the description messages,
    segment is True for the turn that answers a flushed 7.4 segment."""
    texts: tuple
    descriptions: frozenset
    segment: bool


def context(texts, descriptions, segment):
    return StaffContext(tuple(texts), frozenset(descriptions), bool(segment))


def _squash(text):
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _within(span, texts):
    """True when the (squashed) span occurs inside one of the squashed texts."""
    return bool(span) and any(span in text for text in texts)


def _check(fields, args, scope):
    """The field names of args (in the given order) whose span is not inside scope."""
    return [f for f in fields if f in args and not _within(_squash(args[f]), scope)]


@dataclass(frozen=True)
class BoundDraft:
    """One draft of a draft_listing call after span verification: the spans that really
    occur in its source (only those may contribute a value), the field names whose span
    did not, and the texts the backend scans for conflicting values (R-12)."""
    ordinal: int
    source_message: int | None
    spans: dict
    rejected: tuple
    scan: tuple


def bind_draft_listing(args, staff):
    """Verify a draft_listing call. Returns {"error": code} for a call that cannot run,
    else {"drafts": [BoundDraft]}. Several drafts must each name their own text, taken
    from a description message, and those texts may not overlap (R-08)."""
    if staff is None or not staff.segment:
        return {"error": "no_segment"}
    texts = [_squash(t) for t in staff.texts]
    drafts = args["drafts"]
    multi = len(drafts) > 1
    placed = []  # (message number, start, end) of each draft's text
    for draft in drafts:
        if "text" not in draft:
            if multi:
                return {"error": "draft_text_missing"}
            placed.append(None)
            continue
        wanted = _squash(draft["text"])
        for number, body in enumerate(texts, 1):
            if wanted and wanted in body and (not multi or number in staff.descriptions):
                start = body.index(wanted)
                placed.append((number, start, start + len(wanted)))
                break
        else:
            return {"error": "draft_text_not_found"}
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if a and b and a[0] == b[0]:
                shared = min(a[2], b[2]) - max(a[1], b[1])
                if shared > OVERLAP_LIMIT * min(a[2] - a[1], b[2] - b[1]):
                    return {"error": "draft_text_overlap"}
    first = min(staff.descriptions, default=None)
    bound = []
    for ordinal, (draft, where) in enumerate(zip(drafts, placed, strict=True), 1):
        scope = [_squash(draft["text"])] if where else texts
        bad = _check(DRAFT_FIELDS, draft, scope)
        spans = {f: draft[f] for f in DRAFT_FIELDS if f in draft and f not in bad}
        scan = (draft["text"],) if where else tuple(t for t in staff.texts if t)
        bound.append(BoundDraft(ordinal, where[0] if where else first, spans, tuple(bad),
                                scan))
    return {"drafts": bound}


def verify_draft_listing(args, staff):
    """Counts of the verified spans per draft (slice 1 response of draft_listing)."""
    result = bind_draft_listing(args, staff)
    if "error" in result:
        return result
    out = [{"ordinal": d.ordinal, "source_message": d.source_message,
            "verified": len(d.spans), "rejected": list(d.rejected)}
           for d in result["drafts"]]
    return {"drafts": out, "spans_verified": sum(d["verified"] for d in out),
            "spans_rejected": sum(len(d["rejected"]) for d in out)}


def verify_find_listings(args, staff):
    """Criteria spans of a find_listings call (an immediate staff turn only)."""
    error = _immediate(staff)
    if error:
        return {"error": error}
    bad = _check(FIND_FIELDS, args, [_squash(t) for t in staff.texts])
    stated = sum(f in args for f in FIND_FIELDS)
    if stated == len(bad):
        return {"error": "no_verified_span"}
    return {"spans_verified": stated - len(bad), "spans_rejected": len(bad), "rejected": bad}


def verify_propose_update(args, staff):
    """Target code and change spans of a propose_update call. The code only has to look
    like a listing code here; whether it is in the staff text is reported
    (target_in_text) and slice 4b accepts a code that is not in the text only as the
    single find_listings candidate of the same turn."""
    error = _immediate(staff)
    if error:
        return {"error": error}
    match = CODE.fullmatch(args["target"]["code"].strip())
    if match is None:
        return {"error": "invalid_code"}
    changes = args["changes"]
    fields = [c["field"] for c in changes]
    if len(set(fields)) != len(fields):
        return {"error": "duplicate_field"}
    texts = [_squash(t) for t in staff.texts]
    good = [c["field"] for c in changes if _within(_squash(c["span"]), texts)]
    if not good:
        return {"error": "no_verified_span"}
    code = match.group(1).upper()
    in_text = any(m.group(1).upper() == code
                  for t in staff.texts for m in _CODE_IN_TEXT.finditer(t or ""))
    return {"target": code, "target_in_text": in_text, "fields": good,
            "changes_verified": len(good), "changes_rejected": len(changes) - len(good)}


def _immediate(staff):
    if staff is None:
        return "no_staff_context"
    return "segment_turn" if staff.segment else None
