"""Customer tool bodies and taps (Task 10.1; docs/SEARCH.md, docs/RENDERER.md section 1).

One CustomerBinding per customer turn holds the connection, the turn, the gazetteer and the
7.3 requirements binding. Its methods are the bodies app.ai_tools dispatches to, and the
code paths of the customer's button taps (tap(), never the model):

- update(patch): the 7.3 update_requirements body, passed through; it remembers whether
  7.3 told the customer about an inferred purpose in this turn (D5: the renderer then
  does not echo it a second time).
- search_properties: 8.1 search over the stored requirements, the answer-time re-fetch of
  the public rows (R-26) and the 9.1 renderer. Listings, an honest no-match, or one
  question with answer buttons (the 7.3 text question of the same turn is then dropped
  by the engine: one question per turn). The reply payloads go into the turn's plan with
  a SHOWN entry (app.customer_reply), so shown_item is written only after the send.
- send_property_photos: the referenced listing's photos that this conversation has not
  been sent yet (media_sent), in sort order, captioned "#n · CODE".
- get_property: a question about one listing, answered from its public row (app.details);
  the fields come from the customer's own words (details.field_for), never the model.

Every customer-visible fact is rendered from a re-fetched DB row; facts collects what was
rendered for the engine's grounding check (app.grounding.check_payloads). The model sees
positions and codes, never the card texts. Never log requirement values, codes of the
customer's choice or message text.
"""

import logging
import re

from app import (
    ai_tools,
    cards,
    customer_reply,
    details,
    grounding,
    references,
    render,
    requirements,
    search,
    whatsapp,
)
from app.cards import ENGLISH
from app.db import tenant_transaction

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
PHOTOS_PER_ASK = 5  # one "aur pictures" sends at most this many; the next ask the rest
# The 7.3 notices that ask the open question; a rendered clarification replaces them.
QUESTIONS = frozenset({"location_ambiguous", "location_confirm", "location_reask",
                       "purpose_ask"})
# 7.3 acknowledgements the search wrapper line already says.
SAID_BY_RESULTS = frozenset({"requirements_saved", "location_chosen"})
TAP = re.compile(r"(photos|visit|similar|listing):([A-Z0-9]{2,4}-[0-9]{3})"
                 r"|clarify:(location):(\d{1,2}|none)|clarify:(purpose):(sale|rent)"
                 r"|(nomatch):(nearby|budget)|(handoff):(request)")

# New customer wording of 10.1 (awaits owner approval; docs/RENDERER.md section 2).
TEXT = {
    ENGLISH: {"which": "Which property do you mean?", "choose": "Choose"},
    render.ROMAN_URDU: {"which": "Aap kaunsi property keh rahe hain?", "choose": "Chunein"},
}


def _keys(notice):
    notices = notice if isinstance(notice, tuple) else (notice,)
    return {n.key if isinstance(n, ai_tools.Notice) else n for n in notices if n}


def _notice(key, **params):
    return ai_tools.Notice(key, tuple(sorted(params.items())))


def ref_text(item):
    """The ID echo of every reference hit (D3): "#2 · DH6-102", or the code alone."""
    return f"#{item.position} · {item.code}" if item.position else item.code


def conversation_language(conn, turn):
    """The language of a tap turn: the customer's latest typed message (a button title is
    our own wording, not theirs)."""
    with tenant_transaction(conn, turn.agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = conn.execute(
            "SELECT text_body FROM message WHERE conversation_id = %s AND direction = "
            "'inbound' AND message_type = 'text' ORDER BY created_at DESC LIMIT 1",
            (turn.conversation_id,)).fetchone()
    return render.detect_language(row[0] if row else "")


class CustomerBinding:
    def __init__(self, conn, turn, gazetteer, requirements_binding, texts, language=None):
        self.conn, self.turn, self.gazetteer = conn, turn, gazetteer
        self.requirements = requirements_binding
        self.text = "\n".join(text for _, text in texts if text)
        self.wamids = tuple(m.context_wamid for m in turn.messages if m.context_wamid)
        self.purpose_told = False
        self.asked = False      # a clarification with answer buttons is in the reply
        self.results = False    # listing cards are in the reply
        self.facts = grounding.Facts()
        self._language = language

    @property
    def language(self):
        return self._language or render.detect_language(self.text)

    def _result(self, name, response, payloads=(), facts=None, codes=(), notice=None):
        if facts is not None:
            self.facts = self.facts.merge(facts)
        return ai_tools.ToolResult(name, True, response, notice=notice,
                                   payloads=tuple(payloads), trusted_codes=frozenset(codes))

    # -- update_requirements (7.3) --

    def update(self, patch):
        result = self.requirements.update(patch)
        if "purpose_inferred" in _keys(result.notice):
            self.purpose_told = True
        return result

    # -- search_properties --

    def _state(self):
        """(slots, open requirements.Pending or None) of the conversation."""
        with tenant_transaction(self.conn, self.turn.agency_id):
            self.conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            state = self.conn.execute(
                "SELECT slots FROM requirement_state WHERE conversation_id = %s",
                (self.turn.conversation_id,)).fetchone()
            row = self.conn.execute(
                "SELECT id, slot, candidates, created_at, expires_at, unanswered_turns "
                "FROM pending_clarification WHERE conversation_id = %s AND status = 'open'",
                (self.turn.conversation_id,)).fetchone()
        return (state[0] if state else {}), (requirements.Pending(*row) if row else None)

    def search_properties(self, args, context, nearby=False):
        turn, gz = self.turn, self.gazetteer
        result = search.search_conversation(self.conn, turn.agency_id, turn.conversation_id,
                                            gz, nearby=nearby)
        if result.no_neighbours:
            return self._result("search_properties", {"status": "no_neighbours"},
                                notice="nearby_ask")
        data = result.payload(gz)
        response = {"status": data["status"], "total_matches": data["total_matches"],
                    "missing": data["missing"]}
        slots, pending = self._state()
        listings = cards.load_listings(self.conn, turn.agency_id,
                                       [r["listing_code"] for r in data["results"]])
        fallback = cards.fallback_status(self.conn, turn.agency_id, turn.conversation_id)[0]
        try:
            plan = render.render_search(
                data, listings, self.language, request=search.Request.from_slots(slots),
                gazetteer=gz, pending=pending, fallback=fallback, echo=not self.purpose_told)
        except render.GroundingError:
            logger.error("Turn %s: search reply failed grounding; not sent", turn.id)
            return ai_tools.ToolResult("search_properties", False,
                                       {"status": "error", "error": "render_failed"},
                                       error="render_failed", notice="search_unavailable")
        self.asked |= plan.kind == render.CLARIFICATION
        self.results |= plan.kind == render.RESULTS
        payloads = list(plan.payloads)
        if plan.shown and result.search_run_id is not None:
            payloads.append(customer_reply.shown_entry(result.search_run_id, plan.shown))
        response["results"] = [{"position": position, "code": code}
                               for position, _, code in plan.shown]
        return self._result("search_properties", response, payloads, plan.facts,
                            plan.trusted_codes)

    # -- references --

    def _resolve(self, args):
        description = args.get("description")
        if description and not ai_tools.span_in_turn(description, self.text):
            description = None   # R-22: only the customer's own words describe a listing
        return references.resolve(
            self.conn, self.turn.agency_id, self.turn.conversation_id, text=self.text,
            wamids=self.wamids, position=args.get("position"), description=description,
            gazetteer=self.gazetteer)

    def _which(self, name, ref, action):
        """One question listing the options; each row is the listing's own card tap."""
        words = TEXT[self.language]
        rows = [(f"{action}:{i.code}", ref_text(i), None)
                for i in ref.items[:whatsapp.LIST_ROWS_MAX]]
        pairs = {i.position: i.code for i in ref.items if i.position}
        facts = grounding.Facts(codes=frozenset(i.code for i in ref.items),
                                positions=frozenset(pairs), position_codes=pairs)
        payload = whatsapp.list_message(words["which"], words["choose"], [(None, rows)])
        return self._result(name, {"status": "ambiguous", "options": [
            {"position": i.position, "code": i.code} for i in ref.items]},
            [payload], facts, [i.code for i in ref.items])

    def _listing_facts(self, item, extra_money=()):
        listings = cards.load_listings(self.conn, self.turn.agency_id, [item.code])
        positions = [item.position] if item.position and listings else []
        return grounding.facts_for(listings, positions=positions, gazetteer=self.gazetteer,
                                   extra_money=extra_money)

    # -- send_property_photos --

    def send_property_photos(self, args, context, item=None):
        if item is None:
            ref = self._resolve(args)
            if ref.status == references.AMBIGUOUS:
                return self._which("send_property_photos", ref, "photos")
            if ref.status == references.NONE:
                return self._result("send_property_photos", {"status": "not_found"},
                                    notice="listing_unknown")
            item = ref.item
        media = self._unsent_photos(item.property_id)
        response = {"status": "ok", "position": item.position, "code": item.code,
                    "photos": len(media)}
        facts = self._listing_facts(item)
        if not media:
            return self._result("send_property_photos", response, facts=facts,
                                codes=[item.code],
                                notice=_notice("no_more_photos", ref=ref_text(item)))
        entries = [customer_reply.image_entry(item.property_id, m, ref_text(item))
                   for m in media]
        return self._result("send_property_photos", response, entries, facts, [item.code])

    def _unsent_photos(self, property_id):
        """The listing's photos not yet sent in this conversation, in sort order. A shown
        card already carried the cover, so the cover counts as sent once it was shown.
        Public listings only (v_public_listing): a hidden listing has no photos to send."""
        with tenant_transaction(self.conn, self.turn.agency_id):
            self.conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            rows = self.conn.execute(
                """
                SELECT pm.id::text FROM property_media pm
                JOIN v_public_listing v ON v.id = pm.property_id
                JOIN property p ON p.id = pm.property_id
                WHERE pm.property_id = %(p)s AND pm.wa_image_key IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM media_sent ms WHERE ms.conversation_id = %(c)s
                                  AND ms.property_media_id = pm.id)
                  AND NOT (pm.id IS NOT DISTINCT FROM p.cover_media_id AND EXISTS (
                      SELECT 1 FROM shown_item si WHERE si.conversation_id = %(c)s
                        AND si.property_id = pm.property_id))
                ORDER BY pm.sort_order LIMIT %(n)s
                """, {"p": property_id, "c": self.turn.conversation_id,
                      "n": PHOTOS_PER_ASK}).fetchall()
        return [r[0] for r in rows]

    # -- get_property --

    def get_property(self, args, context, item=None):
        if item is None:
            ref = self._resolve(args)
            if ref.status == references.AMBIGUOUS:
                return self._which("get_property", ref, "listing")
            if ref.status == references.NONE:
                return self._result("get_property", {"status": "not_found"},
                                    notice="listing_unknown")
            item = ref.item
        known = details.load_known(self.conn, self.turn.agency_id, item.code)
        fields = details.field_for(self.text)
        payloads = details.answer_detail(known, fields, self.language, item.position)
        money = [v for k, v in (known.public if known else {}).items() if k.endswith("_pkr")
                 or k == "maintenance_pkr_month"]
        return self._result("get_property", {"status": "ok", "position": item.position,
                                             "code": item.code},
                            payloads, self._listing_facts(item, money), [item.code])

    def details_card(self, item):
        """A listing: tap (overflow list row): the listing's card from its public row."""
        listings = cards.load_listings(self.conn, self.turn.agency_id, [item.code])
        if not listings:
            return self._result("get_property", {"status": "not_found"},
                                notice="listing_unknown")
        payload = cards.single_card(listings[0], self.language)
        return self._result("get_property", {"status": "ok"}, [payload],
                            self._listing_facts(item), [item.code])

    # -- taps (handled in code, never sent to the model) --

    def tap(self, reply_id, service):
        """The ToolResults of one customer tap, or None for an id this route does not know.
        service: the 7.3 RequirementService (clarification answers)."""
        match = TAP.fullmatch(reply_id or "")
        if match is None:
            return None
        card, code, loc, choice, purpose, value, nomatch, step, handoff, _ = match.groups()
        if card:
            ref = references.resolve(self.conn, self.turn.agency_id, self.turn.conversation_id,
                                     text=code)
            if ref.status != references.FOUND:
                return [self._result(card, {"status": "not_found"}, notice="listing_unknown")]
            with tenant_transaction(self.conn, self.turn.agency_id):
                references.set_focus(self.conn, self.turn.conversation_id,
                                     ref.item.property_id, "button")
            if card == "photos":
                return [self.send_property_photos({}, None, ref.item)]
            if card == "listing":
                return [self.details_card(ref.item)]
            return [self._result(card, {"status": "not_available"},
                                 notice=f"{card}_unavailable")]
        if handoff:
            return [self._result("request_handoff", dict(ai_tools.STUB_RESPONSE),
                                 notice="handoff_unavailable")]
        if nomatch and step == "budget":
            # "Budget barhayein": ask for the new maximum; never move it by a percentage.
            slots, _ = self._state()
            purpose = (slots.get("purpose") or {}).get("value")
            key = {"rent": "budget_ask_rent", "sale": "budget_ask_sale"}.get(purpose,
                                                                              "budget_ask")
            return [self._result("nomatch", {"status": "asked"}, notice=key)]
        if nomatch:
            return [self.search_properties({}, None, nearby=True)]
        slot, raw = (loc, choice) if loc else (purpose, value)
        answer = None if raw == "none" else (int(raw) if raw.isdigit() else raw)
        notices = service.choose(self.conn, self.turn, slot, answer)
        out = [self._result("clarify", {"status": "ok"}, notice=tuple(notices or ()))]
        return out + [self.search_properties({}, None)]
