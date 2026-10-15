"""Which shown listing a customer means (Task 10.1, D3; R17 references).

resolve() answers "Property 2", "#2", "2nd", "doosri", a quoted (swipe-replied) card, a
listing code, or a description ("Phase 7 wala ghar") against the conversation's latest
shown search (shown_item, written only after a send succeeded). An older search never
counts: "Property 2" is always the 2nd card of the latest results. Order:

1. a quoted card: context_wamid -> outbound message -> shown_item.card_message_id. A
   carousel is one message for all its cards, so it resolves only with a position on it
   or when it showed one listing; otherwise its listings are the options;
2. a listing code in the text (shown, or any public listing of the agency);
3. a position (in the text, else the model's argument);
4. a description: place and property-type words matched among the shown listings;
5. nothing named: the focus listing, or the only shown listing.

Several matches are "ambiguous" with the options (the caller asks with a list); none is
"none". A hit is written to focus. Never log message text or the customer's choice.
"""

import re
from dataclasses import dataclass, field

from app import grounding, requirements
from app.db import tenant_transaction

STATEMENT_TIMEOUT = "5s"
FOUND, AMBIGUOUS, NONE = "found", "ambiguous", "none"
SHOWN_WINDOW = "24 hours"  # = app.search.SHOWN_WINDOW

ORDINALS = {  # not "doosra" / "doosre" / "pehle": they also mean "another" / "before"
    "first": 1, "pehli": 1, "pehla": 1, "second": 2, "doosri": 2, "dusri": 2, "third": 3,
    "teesri": 3, "teesra": 3, "fourth": 4, "chauthi": 4, "chautha": 4, "fifth": 5,
    "paanchvi": 5, "panchvi": 5, "paanchwi": 5, "panchwi": 5,
}
_POSITION = re.compile(
    r"#\s*(\d{1,2})\b|\b(?:property|option|number|no\.?|card|listing)\s*#?\s*(\d{1,2})\b"
    r"|\b(\d{1,2})\s*(?:st|nd|rd|th|wali|wala|wale|number)\b", re.IGNORECASE)
_ORDINAL = re.compile(r"\b(" + "|".join(ORDINALS) + r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class Item:
    position: int | None        # None: a listing that was not in the latest results
    property_id: str
    code: str


@dataclass(frozen=True)
class Ref:
    status: str                 # found | ambiguous | none
    items: tuple = ()           # found: one; ambiguous: the options
    via: str | None = None      # focus.selected_via of a hit

    @property
    def item(self):
        return self.items[0] if self.status == FOUND else None


@dataclass(frozen=True)
class _Shown:
    position: int
    property_id: str
    code: str
    location_id: str
    property_type: str
    card_message_id: str | None


@dataclass
class _Run:
    items: list = field(default_factory=list)

    def at(self, n):
        return next((i for i in self.items if i.position == n), None)


def text_position(text):
    """The one position a text names ("Property 2", "#2", "2nd", "doosri"), else None."""
    found = {int(next(g for g in m.groups() if g)) for m in _POSITION.finditer(text or "")}
    found |= {ORDINALS[m.group(1).casefold()] for m in _ORDINAL.finditer(text or "")}
    return found.pop() if len(found) == 1 else None


def _item(shown):
    return Item(shown.position, shown.property_id, shown.code)


def _latest_run(conn, conversation_id):
    rows = conn.execute(
        f"""
        SELECT si.position, si.property_id::text, p.listing_code, p.location_id::text,
               p.property_type, si.card_message_id::text
        FROM shown_item si JOIN property p ON p.id = si.property_id
        WHERE si.search_run_id = (
            SELECT r.id FROM search_run r
            WHERE r.conversation_id = %s AND r.created_at > now() - interval '{SHOWN_WINDOW}'
              AND EXISTS (SELECT 1 FROM shown_item x WHERE x.search_run_id = r.id)
            ORDER BY r.created_at DESC LIMIT 1)
        ORDER BY si.position
        """, (conversation_id,)).fetchall()
    return _Run([_Shown(*row) for row in rows])


def _quoted(conn, run, wamids):
    """The shown listings on the quoted messages, or None when nothing quoted is a card."""
    if not wamids:
        return None
    ids = {r[0] for r in conn.execute(
        "SELECT message_id::text FROM outbound_message WHERE provider_message_id = ANY(%s) "
        "AND message_id IS NOT NULL", (list(wamids),)).fetchall()}
    on = [i for i in run.items if i.card_message_id in ids]
    return on or None


def _public(conn, code):
    row = conn.execute("SELECT id::text FROM v_public_listing WHERE listing_code = %s",
                       (code,)).fetchone()
    return Item(None, row[0], code) if row else None


def _described(run, description, gazetteer):
    """Shown listings whose place and type fit the description's place / type words."""
    places = [ids for _, ids in grounding.place_mentions(description, gazetteer)]
    types = requirements.parse_types(description)
    if not places and not types:
        return []
    keys = {str(k): k for k in gazetteer.places}

    def fits(item):
        here = keys.get(item.location_id)
        in_place = all(here is not None and any(gazetteer.is_within(here, p) for p in ids)
                       for ids in places)
        return in_place and (not types or item.property_type in types)
    return [i for i in run.items if fits(i)]


def _focus(conn, run, conversation_id):
    row = conn.execute("SELECT property_id::text FROM focus WHERE conversation_id = %s "
                       "AND property_id IS NOT NULL", (conversation_id,)).fetchone()
    if row is None:
        return None
    return next((i for i in run.items if i.property_id == row[0]), None)


def find(conn, conversation_id, *, text="", wamids=(), position=None, description=None,
         gazetteer=None):
    """Ref for a reference, inside the caller's tenant transaction (no write)."""
    run = _latest_run(conn, conversation_id)
    asked = text_position(text) or position
    quoted = _quoted(conn, run, wamids)
    if quoted:
        if len(quoted) == 1:
            return Ref(FOUND, (_item(quoted[0]),), "quote")
        target = run.at(asked) if asked else None
        if target in quoted:
            return Ref(FOUND, (_item(target),), "quote")
        return Ref(AMBIGUOUS, tuple(_item(i) for i in quoted))
    codes = list(dict.fromkeys(grounding.LISTING_CODE.findall(text or "")))
    if len(codes) == 1:
        shown = next((i for i in run.items if i.code == codes[0]), None)
        item = _item(shown) if shown else _public(conn, codes[0])
        return Ref(FOUND, (item,), "description") if item else Ref(NONE)
    if asked:
        shown = run.at(asked)
        return Ref(FOUND, (_item(shown),), "position") if shown else Ref(NONE)
    if description and gazetteer is not None:
        found = _described(run, description, gazetteer)
        if len(found) == 1:
            return Ref(FOUND, (_item(found[0]),), "description")
        if found:
            return Ref(AMBIGUOUS, tuple(_item(i) for i in found))
    focused = _focus(conn, run, conversation_id)
    if focused is not None:
        return Ref(FOUND, (_item(focused),))
    if len(run.items) == 1:
        return Ref(FOUND, (_item(run.items[0]),))
    if run.items:
        return Ref(AMBIGUOUS, tuple(_item(i) for i in run.items))
    return Ref(NONE)


def resolve(conn, agency_id, conversation_id, **kwargs):
    """find() in its own tenant transaction; a hit named by the customer becomes the
    conversation's focus (selected_via)."""
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        ref = find(conn, conversation_id, **kwargs)
        if ref.status == FOUND and ref.via is not None:
            set_focus(conn, conversation_id, ref.item.property_id, ref.via)
    return ref


def set_focus(conn, conversation_id, property_id, via):
    """Inside a tenant transaction: the listing the conversation is about now."""
    conn.execute(
        """
        INSERT INTO focus (agency_id, conversation_id, property_id, selected_via)
        VALUES (current_agency_id(), %s, %s, %s)
        ON CONFLICT (agency_id, conversation_id) DO UPDATE
        SET property_id = EXCLUDED.property_id, selected_via = EXCLUDED.selected_via,
            updated_at = now()
        """, (conversation_id, property_id, via))
