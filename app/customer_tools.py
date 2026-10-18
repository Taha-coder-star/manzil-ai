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
- request_handoff (Task 11.1, R-31): the listing(s) the customer means, then app.handoff
  opens the lead and the pending handoff. Only the customer's own reference (a quoted
  card, a code, a position or description in their words, a tapped button) names a
  listing; focus does not, so "visit karni hai" after several cards asks "Kaunsi property
  ke liye visit?" with every shown listing and "Sab". The reply is one SYSTEM entry (the
  handoff message with the wa.me CTA); everything else of that turn is dropped.

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
    customer_intent,
    customer_reply,
    details,
    grounding,
    handoff,
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
# 7.3 acknowledgements a search reply already says (the results line, the no-match text).
SAID_BY_RESULTS = frozenset({"requirements_saved", "location_chosen"})
# 7.3 notes the voice echo question already says (Task 10.2): it lists the values.
SAID_BY_ECHO = frozenset({"requirements_saved", "purpose_inferred", "budget_implausible"})
# One question per turn (Task 10.2): the 7.3 questions in the existing priority (the search
# gate: an open location / purpose question first; then 7.3's slot order: type, size,
# budget). Only the first one present is sent; the others stay open and are asked later.
QUESTION_ORDER = ("location_ambiguous", "location_confirm", "location_reask", "purpose_ask",
                  "type_unclear", "size_unit_ask", "size_ask", "budget_operator_ask",
                  "budget_unclear", "budget_implausible", "budget_ask")
ECHO_TAP = re.compile(r"echo:(?:yes:(\d{1,9})|edit)")
KEEP_TAP = re.compile(r"keep:(yes|no):([0-9a-f]{8})")
# Task 11.1: a row of the "which property" question: the reason it was asked for, then
# the listing or "all" (the shown ones, R-31 "Dono").
_REASONS = "|".join(handoff.REASONS)
HANDOFF_TAP = re.compile(r"handoff:(" + _REASONS + r"):([A-Z0-9]{2,4}-[0-9]{3}|all)")
TAP = re.compile(r"(photos|visit|similar|listing):([A-Z0-9]{2,4}-[0-9]{3})"
                 r"|clarify:(location):(\d{1,2}|none)|clarify:(purpose):(sale|rent)"
                 r"|(nomatch):(nearby|budget)|(handoff):(request)|echo:(?:yes:\d{1,9}|edit)"
                 r"|keep:(?:yes|no):[0-9a-f]{8}"
                 r"|handoff:(?:" + _REASONS + r"):(?:[A-Z0-9]{2,4}-[0-9]{3}|all)")

# New customer wording of 10.1 (awaits owner approval; docs/RENDERER.md section 2).
TEXT = {
    ENGLISH: {"which": "Which property do you mean?", "choose": "Choose",
              # Task 11.1 (awaits owner approval)
              "which_visit": "Which property is the visit for?", "all": "All of them"},
    render.ROMAN_URDU: {"which": "Aap kaunsi property keh rahe hain?", "choose": "Chunein",
                        "which_visit": "Kaunsi property ke liye visit?", "all": "Sab"},
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
        self.echoed = False     # the voice echo question is in the reply (Task 10.2)
        self.results = False    # listings or the honest no-match are in the reply
        self.handoff = None     # Task 11.1: the SYSTEM entry of a handoff in this turn
        self.place_unresolved = False   # this turn's place was not found: no search yet
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
        # Live 2026-10-18: "Block 6 main flat chahiye" (place not found) was answered with
        # a search of the OLD stored place. A place that was not found is asked about; it
        # holds the search unless the same message also named a place that was found.
        places = [op["status"] for op in (result.response or {}).get("ops", ())
                  if op.get("slot") == "location"]
        if "applied" in places:
            self.place_unresolved = False
        elif "unknown" in places or "out_of_coverage" in places:
            self.place_unresolved = True
        return result

    # -- search_properties --

    def _state(self):
        """(slots, open requirements.Pending or None, state version) of the conversation."""
        with tenant_transaction(self.conn, self.turn.agency_id):
            self.conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            state = self.conn.execute(
                "SELECT slots, version FROM requirement_state WHERE conversation_id = %s",
                (self.turn.conversation_id,)).fetchone()
            row = self.conn.execute(
                "SELECT id, slot, candidates, created_at, expires_at, unanswered_turns "
                "FROM pending_clarification WHERE conversation_id = %s AND status = 'open'",
                (self.turn.conversation_id,)).fetchone()
        return ((state[0] if state else {}), (requirements.Pending(*row) if row else None),
                state[1] if state else None)

    def search_properties(self, args, context, nearby=False):
        if self.place_unresolved:   # the update's own notice already asks for the place
            return self._result("search_properties",
                                {"status": "answer_first", "needs_answer": ["location"]})
        turn, gz = self.turn, self.gazetteer
        slots, pending, version = self._state()
        if pending is None:
            # Task 10.2: an unanswered type / size / budget question (this turn or an
            # earlier one) is asked, never searched past with an older or missing value.
            asks = self.requirements.open_asks()
            if asks:
                return self._result("search_properties",
                                    {"status": "answer_first",
                                     "needs_answer": [slot for slot, _ in asks]},
                                    notice=tuple(asks[0][1]))
            stored, inherited, token = self.requirements.inherited_state()
            if inherited:
                # Filters from an earlier session are asked about, never searched unconfirmed.
                return self._keep_question(stored, inherited, token)
            if requirements.uncertain_slots(slots):
                return self._voice_echo(slots, version)
        result = search.search_conversation(self.conn, turn.agency_id, turn.conversation_id,
                                            gz, nearby=nearby)
        if result.no_neighbours:
            return self._result("search_properties", {"status": "no_neighbours"},
                                notice="nearby_ask")
        data = result.payload(gz)
        response = {"status": data["status"], "total_matches": data["total_matches"],
                    "missing": data["missing"]}
        slots, pending, _ = self._state()
        listings = cards.load_listings(self.conn, turn.agency_id,
                                       [r["listing_code"] for r in data["results"]])
        fallback = cards.fallback_status(self.conn, turn.agency_id, turn.conversation_id)[0]
        try:
            plan = render.render_search(
                data, listings, self.language,
                request=result.request or search.Request.from_slots(slots), gazetteer=gz, pending=pending, fallback=fallback, echo=not self.purpose_told)
        except render.GroundingError:
            logger.error("Turn %s: search reply failed grounding; not sent", turn.id)
            return ai_tools.ToolResult("search_properties", False,
                                       {"status": "error", "error": "render_failed"},
                                       error="render_failed", notice="search_unavailable")
        self.asked |= plan.kind == render.CLARIFICATION
        self.results |= plan.kind in (render.RESULTS, render.NO_MATCH_KIND)
        payloads = list(plan.payloads)
        if plan.shown and result.search_run_id is not None:
            payloads.append(customer_reply.shown_entry(result.search_run_id, plan.shown))
        response["results"] = [{"position": position, "code": code}
                               for position, _, code in plan.shown]
        return self._result("search_properties", response, payloads, plan.facts,
                            plan.trusted_codes)

    def _keep_question(self, slots, names, token):
        """The filters of an earlier session, asked about with [Haan, wohi] / [Nayi search]
        (the taps are tap()'s keep:yes / keep:no). Nothing is searched until they answer."""
        old = {name: slots[name] for name in names}
        again = self.requirements.keep_asked(token)   # asked before: a short reminder
        request = search.Request.from_slots(old)
        types = (old.get("property_types") or {}).get("value") or []
        category = (old.get("category") or {}).get("value")
        facts = render.request_facts(request, self.gazetteer)
        try:
            plan = render.keep_question(request, types, self.language, self.gazetteer, facts,
                                        token, again, category)
        except render.GroundingError:
            logger.error("Turn %s: keep question failed grounding; not sent", self.turn.id)
            return ai_tools.ToolResult("search_properties", False,
                                       {"status": "error", "error": "render_failed"},
                                       error="render_failed", notice="search_unavailable")
        self.asked = True
        return self._result("search_properties",
                            {"status": "answer_first", "needs_answer": ["inherited_filters"]},
                            plan.payloads, plan.facts)

    def question_open(self):
        """A 7.3 question is waiting for the customer (an open clarification, or an
        unanswered type / size / budget question): this turn's reply asks it again."""
        _, pending, _ = self._state()
        return pending is not None or bool(self.requirements.open_asks())

    def filters_text(self):
        """The filters the search would use now (the acknowledgement shows them): stored
        values only, without those still waiting for the customer's answer."""
        slots, _, _ = self._state()
        waiting = set(self.requirements.inherited())
        active = {name: entry for name, entry in slots.items() if name not in waiting}
        types = (active.get("property_types") or {}).get("value") or []
        category = (active.get("category") or {}).get("value")
        return render.filters_line(search.Request.from_slots(active), types, self.language,
                                   self.gazetteer, category)

    def keep_answer(self, answer, service, token=None):
        """[Haan, wohi] / [Nayi search] (a tap, token = the question's) or the same typed
        (token None: the question open in this conversation). The ToolResults: the
        acknowledgement, then the search (which asks the next question, if any)."""
        if answer == "yes":
            notices = service.keep_inherited(self.conn, self.turn, token)
        else:
            notices = service.fresh_start(self.conn, self.turn, "inherited", token)
        if notices is None:
            return [self._result("keep", {"status": "stale"}, notice="echo_stale")]
        if "purpose_inferred" in _keys(tuple(notices)):
            self.purpose_told = True
        return [self._result("keep", {"status": "ok"}, notice=tuple(notices)),
                self.search_properties({}, None)]

    def _voice_echo(self, slots, version):
        """R16 7.4: a heard, uncertain price / size / location / purpose is echoed with
        [Haan] / [Badlein] before any search; no search_run, nothing shown."""
        try:
            plan = render.voice_echo(search.Request.from_slots(slots), self.language,
                                     self.gazetteer, version)
        except render.GroundingError:
            logger.error("Turn %s: voice echo failed grounding; not sent", self.turn.id)
            return ai_tools.ToolResult("search_properties", False,
                                       {"status": "error", "error": "render_failed"},
                                       error="render_failed", notice="search_unavailable")
        self.asked = self.echoed = True
        return self._result("search_properties", {"status": "confirm_first"}, plan.payloads,
                            plan.facts)

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

    def show_listing(self, code):
        """A listing code the customer asked to see: the card of an ACTIVE listing, one fixed
        line for any other code (sold, reserved, withdrawn and unknown alike: the status is
        never said). Live 2026-10-18."""
        ref = references.resolve(self.conn, self.turn.agency_id, self.turn.conversation_id,
                                 text=code)
        if ref.status == references.FOUND:
            return self.details_card(ref.item)
        return self._result("get_property", {"status": "not_found"},
                            notice=_notice("listing_unavailable", code=code))

    # -- request_handoff (Task 11.1) --

    def request_handoff(self, args, context, items=None, reason=None):
        reason = reason or args.get("reason") or "agent"
        if (items is None and details.LEGAL in details.field_for(self.text)
                and not customer_intent.asks_for_agent(self.text)):
            # Live 2026-10-18: "Is ghar ke documents clear hain?" opened the handoff list (one
            # tap = a lead nobody asked for). A documents question gets the agent offer; the
            # [Agent se baat] button (or the customer asking for one) opens the handoff.
            return self._result("request_handoff", {"status": "offered", "reason": "legal"},
                                details.answer_detail(None, [details.LEGAL], self.language))
        if items is None:
            # Only the customer's words name a listing here (R-31): no model position, no
            # focus. Several shown and nothing named -> ask.
            ref = self._resolve({"description": args.get("description")})
            if ref.status == references.FOUND and ref.via in ("quote", "position",
                                                               "description"):
                items = ref.items
            elif ref.status == references.AMBIGUOUS and (self.wamids
                                                          or args.get("description")):
                return self._which_handoff(ref.items, reason)
            else:
                items = self._shown()
                if len(items) > 1:
                    return self._which_handoff(items, reason)
        with tenant_transaction(self.conn, self.turn.agency_id):
            opened = handoff.open_handoff(self.conn, self.turn.conversation_id, items, reason)
        if opened.status == handoff.NO_AGENT:
            return self._result("request_handoff", {"status": "no_agent"},
                                notice="handoff_no_agent")
        self.handoff = customer_reply.system_entry(
            handoff.customer_message(opened, self.language))
        return self._result("request_handoff", {"status": opened.status,
                                                "lead": opened.lead_code},
                            [self.handoff], codes=[i.code for i in items])

    def _shown(self):
        with tenant_transaction(self.conn, self.turn.agency_id):
            self.conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            return references.shown_items(self.conn, self.turn.conversation_id)

    def _which_handoff(self, items, reason):
        """R-31: "Kaunsi property ke liye visit?" with every option and "Sab"."""
        words = TEXT[self.language]
        rows = [(f"handoff:{reason}:{i.code}", ref_text(i), None)
                for i in items[:whatsapp.LIST_ROWS_MAX - 1]]
        rows.append((f"handoff:{reason}:all", words["all"], None))
        pairs = {i.position: i.code for i in items if i.position}
        facts = grounding.Facts(codes=frozenset(i.code for i in items),
                                positions=frozenset(pairs), position_codes=pairs)
        question = words["which_visit"] if reason == "visit" else words["which"]
        payload = whatsapp.list_message(question, words["choose"], [(None, rows)])
        self.asked = True
        return self._result("request_handoff", {"status": "ambiguous", "options": [
            {"position": i.position, "code": i.code} for i in items]},
            [payload], facts, [i.code for i in items])

    def _handoff_tap(self, reason, target):
        if target == "all":
            items = self._shown()
        else:
            ref = references.resolve(self.conn, self.turn.agency_id,
                                     self.turn.conversation_id, text=target)
            if ref.status != references.FOUND:
                return [self._result("request_handoff", {"status": "not_found"},
                                     notice="listing_unknown")]
            items = ref.items
        return [self.request_handoff({}, None, items=items, reason=reason)]

    # -- taps (handled in code, never sent to the model) --

    def tap(self, reply_id, service):
        """The ToolResults of one customer tap, or None for an id this route does not know.
        service: the 7.3 RequirementService (clarification answers)."""
        match = TAP.fullmatch(reply_id or "")
        if match is None:
            return None
        chosen = HANDOFF_TAP.fullmatch(reply_id)
        if chosen:
            return self._handoff_tap(*chosen.groups())
        keep = KEEP_TAP.fullmatch(reply_id)
        if keep:
            return self.keep_answer(keep.group(1), service, keep.group(2))
        echo = ECHO_TAP.fullmatch(reply_id)
        if echo and echo.group(1) is None:   # [Badlein]: the customer sends the correction
            return [self._result("echo", {"status": "edit"}, notice="echo_edit")]
        if echo:
            # [Haan]: confirm what the question showed, then search. A tap on an older
            # question (the values changed, it was already answered, or its message was
            # sent over 24 h ago) confirms nothing and searches nothing.
            quoted = self.turn.messages[-1].context_wamid   # the question message tapped
            if service.confirm_echo(self.conn, self.turn, int(echo.group(1)), quoted) == "stale":
                return [self._result("echo", {"status": "stale"}, notice="echo_stale")]
            return [self.search_properties({}, None)]
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
            if card == "visit":   # the card's own button names the listing (R-31)
                return [self.request_handoff({}, None, items=ref.items, reason="visit")]
            return [self._result(card, {"status": "not_available"},
                                 notice=f"{card}_unavailable")]
        if handoff:   # [Agent se baat]: the same resolution as the model's request
            return [self.request_handoff({}, None)]
        if nomatch and step == "budget":
            # "Budget barhayein": ask for the new maximum; never move it by a percentage.
            slots, _, _ = self._state()
            purpose = (slots.get("purpose") or {}).get("value")
            key = {"rent": "budget_ask_rent", "sale": "budget_ask_sale"}.get(purpose,
                                                                              "budget_ask")
            return [self._result("nomatch", {"status": "asked"}, notice=key)]
        if nomatch:
            return [self.search_properties({}, None, nearby=True)]
        slot, raw = (loc, choice) if loc else (purpose, value)
        answer = None if raw == "none" else (int(raw) if raw.isdigit() else raw)
        notices = service.choose(self.conn, self.turn, slot, answer)
        if notices is None:   # an older question, or no such option: nothing changed
            return [self._result("clarify", {"status": "stale"}, notice="echo_stale")]
        out = [self._result("clarify", {"status": "ok"}, notice=tuple(notices))]
        return out + [self.search_properties({}, None)]
