"""Customer tool bodies (Task 10.1; docs/SEARCH.md, docs/RENDERER.md section 1).

One CustomerBinding per customer turn holds the connection, the turn, the gazetteer and the
7.3 requirements binding. Its methods are the bodies app.ai_tools dispatches to:

- update(patch): the 7.3 update_requirements body, passed through; it remembers whether
  7.3 told the customer about an inferred purpose in this turn (D5: the renderer then
  does not echo it a second time).
- search_properties: 8.1 search over the stored requirements, the answer-time re-fetch of
  the public rows (R-26) and the 9.1 renderer. The reply payloads go into the turn's plan
  with a SHOWN entry (app.customer_reply), so shown_item is written only after the cards
  were sent. The model sees positions and codes, never the card texts.

Never log requirement values, codes of the customer's choice or message text.
"""

import logging

from app import ai_tools, cards, customer_reply, render, requirements, search
from app.db import tenant_transaction

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"


def _keys(notice):
    notices = notice if isinstance(notice, tuple) else (notice,)
    return {n.key if isinstance(n, ai_tools.Notice) else n for n in notices if n}


class CustomerBinding:
    def __init__(self, conn, turn, gazetteer, requirements_binding, texts):
        self.conn, self.turn, self.gazetteer = conn, turn, gazetteer
        self.requirements = requirements_binding
        self.text = "\n".join(text for _, text in texts if text)
        self.purpose_told = False

    @property
    def language(self):
        return render.detect_language(self.text)

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

    def search_properties(self, args, context):
        turn, gz = self.turn, self.gazetteer
        result = search.search_conversation(self.conn, turn.agency_id, turn.conversation_id, gz)
        data = result.payload(gz)
        response = {"status": data["status"], "total_matches": data["total_matches"],
                    "missing": data["missing"]}
        if "pending_clarification" in data["missing"]:
            # The open question was already put to the customer by update_requirements
            # (7.3 notice or re-ask): one question per turn, nothing more to send.
            return ai_tools.ToolResult("search_properties", True, response)
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
        payloads = list(plan.payloads)
        if plan.shown and result.search_run_id is not None:
            payloads.append(customer_reply.shown_entry(result.search_run_id, plan.shown))
        response["results"] = [{"position": position, "code": code}
                               for position, _, code in plan.shown]
        return ai_tools.ToolResult("search_properties", True, response,
                                   payloads=tuple(payloads),
                                   trusted_codes=frozenset(plan.trusted_codes))
