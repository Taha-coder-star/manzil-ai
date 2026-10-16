"""Customer search renderer (Task 9.1, C11; Red-Team R-21; D9, D15; docs/RENDERER.md).

render_search turns one 8.1 search result into the WhatsApp payloads of a customer
reply. Every property fact comes from a template over a re-fetched public row; no model
text is used (owner decision 2026-10-14: the wrapper line is a template too).

    plan = render_search(result.payload(gazetteer), cards.load_listings(conn, agency_id,
                         codes), language, request=search.Request.from_slots(slots),
                         gazetteer=gazetteer, pending=pending)

The 10.1 boundary (answer-time re-fetch, R-26): the caller loads the listings from
v_public_listing (cards.load_listings, tenant transaction, ACTIVE only) immediately before
rendering. Card facts come from those rows, never from the search payload; a result
without a row (sold, reserved, withdrawn since the search) is not rendered, is reported
in RenderPlan.dropped, and the positions are renumbered so "#n" is what was sent.
RenderPlan.shown is what 10.1 passes to search.record_shown after a successful send.

Pure: no database, no model, no clock. Every plan passes the grounding validator over
all of its texts (app.grounding.check_payloads); a template that fails it is a bug and
raises GroundingError instead of sending.
"""

import re
from dataclasses import dataclass, field

from app import cards, grounding, whatsapp
from app.cards import ENGLISH, LANGUAGES, ROMAN_URDU

OK, NO_MATCH, CLARIFY = "ok", "no_match", "clarification_needed"  # = app.search
RESULTS, NO_MATCH_KIND, CLARIFICATION = "results", "no_match", "clarification"
# Task 10.2: a question about values already stored (unconfirmed), not about a missing or
# ambiguous one: the customer confirms or changes; nothing is searched until then.
CONFIRMATION = "confirmation"

# Button ids: the 10.1 tap handlers key on these prefixes; no location or property id is
# put in a clarification button (the index points into the stored pending candidates).
NEARBY, MORE_BUDGET, HANDOFF = "nomatch:nearby", "nomatch:budget", "handoff:request"
CLARIFY_LOCATION, CLARIFY_NONE = "clarify:location:{}", "clarify:location:none"
CLARIFY_PURPOSE = "clarify:purpose:{}"
# Task 10.2 voice echo: [Haan] carries the requirement_state version it was rendered from.
ECHO_YES, ECHO_EDIT = "echo:yes:{}", "echo:edit"

# All customer-facing wording of this module (awaits owner approval; docs/RENDERER.md).
TEXT = {
    ENGLISH: {
        "wrapper": "Here are {things}{place}{budget}:",
        "in_place": " in {place}",
        "max": ", up to {amount}", "around": ", around {amount}", "min": ", from {amount}",
        "range": ", {low} to {high}", "cheaper": ", below the earlier price",
        "per_month": " per month",
        "neighbour": "{place} -- nearby", "above_budget": "slightly above budget",
        "size": "size differs a little", "beds": "fewer bedrooms",
        "echo_purpose_sale": "These are for sale -- tell me if you meant rent.",
        "echo_purpose_rent": "These are for rent -- tell me if you want to buy.",
        "echo_budget": "I took your budget as {budget} -- correct me if that's wrong.",
        "echo_location": "I took the location as {place} -- correct me if that's wrong.",
        "echo_size": "I took the size as {size} -- correct me if that's wrong.",
        "no_match_cheaper": "No cheaper{what}{place} are available right now.",
        "no_match": "No{what}{place} match this budget right now.",
        "lowest": "Prices there start from {amount}.",
        "nearby": "Nearby area", "more_budget": "Raise budget", "agent": "Talk to agent",
        "which_place": "Which place do you mean?",
        "confirm_place": "Did you mean {place}?", "yes": "Yes", "no_place": "No, another place",
        "choose_place": "Choose place",
        "purpose_ask": "Do you want to buy or rent?", "buy": "Buy", "rent": "Rent",
        "location_ask": "Which area are you looking in?",
        "type_ask": "What type of property (house, flat, plot, office) and what budget?",
        "generic_ask": "Please tell me a little more about what you are looking for.",
        "voice_echo": "You said: {facts} -- is that right?", "echo_yes": "Yes",
        "echo_edit": "Change", "to_sale": "to buy", "to_rent": "to rent",
    },
    ROMAN_URDU: {
        "wrapper": "Ji, {place}{things}{budget} -- ye options hain:",
        "in_place": "{place} mein ",
        "max": ", {amount} tak", "around": ", {amount} ke aas paas", "min": ", {amount} se upar",
        "range": ", {low} se {high} tak", "cheaper": ", pehle se kam price par",
        "per_month": " mahana",
        "neighbour": "{place} -- qareeb", "above_budget": "budget se thoda upar",
        "size": "size thoda alag", "beds": "bedroom kam",
        "echo_purpose_sale": "Ye khareedne ke options hain -- agar kiraye par chahiye to "
                             "bata dein.",
        "echo_purpose_rent": "Ye kiraye ke options hain -- agar khareedna hai to bata dein.",
        "echo_budget": "Budget main ne {budget} samjha hai -- ghalat ho to bata dein.",
        "echo_location": "Jagah main ne {place} samjhi hai -- ghalat ho to bata dein.",
        "echo_size": "Size main ne {size} samjha hai -- ghalat ho to bata dein.",
        "no_match_cheaper": "{place}is se kam price ka{what} abhi available nahi.",
        "no_match": "{place}is budget mein{what} abhi available nahi.",
        "lowest": "Yahan price {amount} se shuru hoti hai.",
        "nearby": "Nearby area", "more_budget": "Budget barhayein", "agent": "Agent se baat",
        "which_place": "Aap ka matlab kaunsi jagah hai?",
        "confirm_place": "Kya aap {place} keh rahe hain?", "yes": "Haan",
        "no_place": "Nahi, aur jagah", "choose_place": "Jagah chunein",
        "purpose_ask": "Aap property khareedna chahte hain ya kiraye par lena chahte hain?",
        "buy": "Khareedna", "rent": "Kiraye par",
        "location_ask": "Aap kis area mein property dekh rahe hain?",
        "type_ask": ("Kis qisam ki property chahiye (ghar, flat, plot, office) aur budget "
                     "kitna hai?"),
        "generic_ask": "Please thora aur bata dein ke aap ko kya chahiye.",
        "voice_echo": "Aap ne kaha: {facts} -- sahi hai?", "echo_yes": "Haan",
        "echo_edit": "Badlein", "to_sale": "khareedne ke liye", "to_rent": "kiraye par",
    },
}
TYPE_ONE = {ROMAN_URDU: {"house": "ghar", "apartment": "flat", "shop": "dukaan",
                         "warehouse": "godown"}}
TYPE_MANY = {ENGLISH: {"house": "houses", "apartment": "apartments", "penthouse": "penthouses",
                       "portion": "portions", "office": "offices", "shop": "shops",
                       "plot": "plots", "showroom": "showrooms", "warehouse": "warehouses",
                       "building": "buildings", "floor": "floors"}}
SIZE_UNITS = {ROMAN_URDU: {"sq_yd": "gaz"}}

# Language decision (D15: mirror the customer). Both languages are Latin script, so the
# choice is by vocabulary; Roman Urdu is the default for mixed or unclear text.
ENGLISH_WORDS = frozenset((
    "i", "i'm", "im", "am", "looking", "for", "the", "an", "in", "on", "of", "around", "want",
    "need", "please", "show", "me", "what", "is", "are", "any", "with", "and", "to", "my",
    "have", "do", "you", "can", "near", "under", "per", "month", "thanks", "hello", "would",
    "like", "buy", "sell", "price", "how", "much", "much", "cheaper", "some", "there", "this",
    "that", "it", "be", "about", "find", "looking", "will", "your", "if", "at", "from",
))
URDU_WORDS = frozenset((
    "mein", "main", "ka", "ki", "ke", "hai", "hain", "chahiye", "dikhao", "aur", "kya", "nahi",
    "se", "tak", "wala", "wali", "ghar", "mujhe", "bhi", "kuch", "sasta", "karni", "kar",
    "ho", "ji", "theek", "acha", "gaz", "kiraye", "kiraya", "khareedna", "batao", "bata",
    "koi", "aap", "hum", "ye", "wo", "kab", "kitna", "kitne", "par", "pe", "ko", "sakta",
    "chahta", "chahti", "dekho", "bhejo", "karein", "hoon", "tha", "thi", "assalam",
))
_WORDS = re.compile(r"[a-z']+")
_URDU_SCRIPT = re.compile(r"[؀-ۿ]")


class GroundingError(RuntimeError):
    """A rendered template stated something the rendered rows do not support (a bug)."""


@dataclass(frozen=True)
class RenderPlan:
    payloads: list                 # ordered WhatsApp payloads (cards.fixed_plan / send_plan)
    kind: str                      # results | no_match | clarification
    language: str
    shown: list = field(default_factory=list)  # [(position, property_id, code)] rendered
    trusted_codes: frozenset = frozenset()     # codes turn_engine.ground_reply may allow
    facts: grounding.Facts = field(default_factory=grounding.Facts)
    dropped: list = field(default_factory=list)  # payload codes with no public row now


def detect_language(text, preferred=None):
    """english or roman_urdu: an explicit preference wins; otherwise English only when the
    text is clearly English (at least 2 English words and more English than Roman Urdu)."""
    if preferred in LANGUAGES:
        return preferred
    text = text or ""
    if _URDU_SCRIPT.search(text):
        return ROMAN_URDU  # Urdu-script replies are on the cut list (Demo Scope 3)
    words = _WORDS.findall(text.casefold())
    english = sum(w in ENGLISH_WORDS for w in words)
    urdu = sum(w in URDU_WORDS for w in words)
    return ENGLISH if english >= 2 and english > urdu else ROMAN_URDU


# --- wording helpers ------------------------------------------------------------------------

def amount_text(pkr):
    """"8 crore", "5 lakh", "85,000": an amount as the cards write it, without "PKR"."""
    return cards.price_text(int(pkr), None).removeprefix("PKR ")


def _place_names(request, gazetteer):
    if request is None or gazetteer is None:
        return []
    ids = {str(k): k for k in gazetteer.places}
    return [gazetteer.places[ids[n]].display_name for n in request.nodes if n in ids]


def _short_place(display, gazetteer):
    """"Phase 7" for "DHA Phase 7" (the label of a neighbour card, Demo Scope scenario 2)."""
    if gazetteer is not None:
        for place in gazetteer.places.values():
            if place.display_name == display:
                return place.name
    return display


def _size_text(request, language):
    size = (request.size or {}) if request is not None else {}
    if size.get("value") is None or not size.get("unit"):
        return None
    unit = SIZE_UNITS.get(language, {}).get(size["unit"],
                                            cards.UNITS.get(size["unit"], size["unit"]))
    return f"{cards._number(size['value'])} {unit}"


def _things(request, language):
    types = list(request.types) if request is not None else []
    if language == ENGLISH:
        words = [TYPE_MANY[ENGLISH].get(t, t) for t in types] or ["properties"]
        thing = " / ".join(words)
        if request is not None and request.purpose in ("sale", "rent"):
            thing += f" for {request.purpose}"
        return thing
    return " / ".join(TYPE_ONE[ROMAN_URDU].get(t, t) for t in types) or "property"


def _budget_text(request, language):
    if request is None:
        return ""
    words = TEXT[language]
    if request.no_budget_relaxation:  # after "kuch sasta" (Demo Scope rule clarification)
        return words["cheaper"]
    low, high, stated = request.budget_min, request.budget_max, request.stated
    if low is not None and high is not None and stated is not None:
        text = words["around"].format(amount=amount_text(stated))
    elif low is not None and high is not None:
        text = words["range"].format(low=amount_text(low), high=amount_text(high))
    elif high is not None:
        text = words["max"].format(amount=amount_text(high))
    elif low is not None:
        text = words["min"].format(amount=amount_text(low))
    else:
        return ""
    return text + (words["per_month"] if request.purpose == "rent" else "")


def wrapper_line(request, language, gazetteer=None):
    """The templated wrapper (R-21: the model writes no facts). Roman Urdu follows Demo
    Scope scenario 2: "Ji, DHA Phase 6 mein 500 gaz, 8 crore tak -- ye options hain:"."""
    words = TEXT[language]
    names = " / ".join(_place_names(request, gazetteer))
    if language == ENGLISH:
        size = _size_text(request, language)
        things = (f"{size} " if size else "") + _things(request, language)
        place = words["in_place"].format(place=names) if names else ""
        return words["wrapper"].format(things=things, place=place,
                                       budget=_budget_text(request, language))
    place = words["in_place"].format(place=names) if names else ""
    things = _size_text(request, language) or _things(request, language)
    return words["wrapper"].format(place=place, things=things,
                                   budget=_budget_text(request, language))


def requirements_line(request, gazetteer=None):
    """The stored requirements for a broker's lead card (Task 11.1; staff cards are English,
    D15): "500 sq yd houses for sale in DHA Phase 6, up to 8 crore"; None when nothing
    is stored."""
    if request is None:
        return None
    names = " / ".join(_place_names(request, gazetteer))
    size = _size_text(request, ENGLISH)
    budget = _budget_text(request, ENGLISH)
    if not (names or size or budget or request.types or request.purpose):
        return None
    things = (f"{size} " if size else "") + _things(request, ENGLISH)
    place = TEXT[ENGLISH]["in_place"].format(place=names) if names else ""
    return f"{things}{place}{budget}"


def echo_lines(payload, request, language, gazetteer=None):
    """One line per unconfirmed slot the search echoed (SEARCH.md: never blocking)."""
    words, out = TEXT[language], []
    for item in payload.get("echo") or ():
        slot = item.get("slot")
        if slot == "purpose" and request is not None and request.purpose in ("sale", "rent"):
            out.append(words[f"echo_purpose_{request.purpose}"])
        elif slot == "budget" and request is not None and _budget_text(request, language):
            out.append(words["echo_budget"].format(
                budget=_budget_text(request, language).lstrip(", ")))
        elif slot == "location" and _place_names(request, gazetteer):
            out.append(words["echo_location"].format(
                place=" / ".join(_place_names(request, gazetteer))))
        elif slot == "size" and _size_text(request, language):
            out.append(words["echo_size"].format(size=_size_text(request, language)))
    return out


def result_labels(result, language, gazetteer=None):
    """The labels of one payload result (relaxations are never silent, R17 step 7)."""
    words, labels = TEXT[language], []
    if result.get("location_match") == "neighbour" and result.get("location"):
        labels.append(words["neighbour"].format(
            place=_short_place(result["location"], gazetteer)))
    if result.get("above_budget") or "budget" in (result.get("relaxations") or ()):
        labels.append(words["above_budget"])
    for step in ("size", "beds"):
        if step in (result.get("relaxations") or ()):
            labels.append(words[step])
    return tuple(labels)


# --- the three outcomes -------------------------------------------------------------------

def render_search(payload, listings, language, *, request=None, gazetteer=None, pending=None,
                  fallback=False, echo=True):
    """The customer reply for one search payload (search.Result.payload). listings: the
    re-fetched cards.Listing rows of the payload's codes (any order); request: the
    search.Request the search ran on (wrapper, echo, no-match wording); pending: the open
    requirements.Pending (clarification buttons); fallback: the per-contact carousel
    fallback flag."""
    if language not in LANGUAGES:
        raise ValueError(f"unknown language {language!r}")
    status = payload.get("status")
    if status == OK:
        plan = _results(payload, listings, language, request, gazetteer, fallback, echo)
    elif status == NO_MATCH:
        plan = _no_match(payload, language, request, gazetteer)
    elif status == CLARIFY:
        plan = _clarification(payload, language, pending, gazetteer)
    else:
        raise ValueError(f"unknown search status {status!r}")
    found = grounding.check_payloads(plan.payloads, plan.facts, gazetteer)
    if found:
        raise GroundingError(f"template stated unsupported facts: {found}")
    return plan


def _results(payload, listings, language, request, gazetteer, fallback, echo):
    by_code = {item.code: item for item in listings}
    rendered, shown, labels, dropped = [], [], {}, []
    for result in payload.get("results") or ():
        item = by_code.get(result["listing_code"])
        if item is None:
            dropped.append(result["listing_code"])
            continue
        rendered.append(item)
        shown.append((len(rendered), result["id"], item.code))
        labels[item.code] = result_labels(result, language, gazetteer)
    if not rendered:  # everything changed since the search: an honest no-match
        plan = _no_match({**payload, "lowest_price_pkr": None}, language, request, gazetteer)
        return RenderPlan(plan.payloads, plan.kind, language, facts=plan.facts,
                          dropped=dropped)
    lines = [wrapper_line(request, language, gazetteer)]
    if echo:
        lines += echo_lines(payload, request, language, gazetteer)
    payloads = cards.render(rendered, fallback=fallback, language=language, labels=labels,
                            note="\n".join(lines))
    facts = grounding.facts_for(rendered, positions=[n for n, _, _ in shown],
                                request=request, gazetteer=gazetteer)
    return RenderPlan(payloads, RESULTS, language, shown=shown,
                      trusted_codes=frozenset(item.code for item in rendered), facts=facts,
                      dropped=dropped)


def _no_match(payload, language, request, gazetteer):
    words = TEXT[language]
    names = " / ".join(_place_names(request, gazetteer))
    size = _size_text(request, language)
    if language == ENGLISH:
        what = (f" {size} " if size else " ") + _things(request, language)
        place = f" in {names}" if names else ""
    else:
        thing = _things(request, language)
        what = f" {size} {thing}" if size else f" {thing}"
        place = f"{names} mein " if names else ""
    key = "no_match_cheaper" if request is not None and request.no_budget_relaxation \
        else "no_match"
    lines = [words[key].format(what=what, place=place)]
    lowest = payload.get("lowest_price_pkr")
    if lowest:
        lines.append(words["lowest"].format(amount=amount_text(lowest)))
    body = " ".join(lines)
    body = body[0].upper() + body[1:]
    buttons = [(NEARBY, words["nearby"]), (MORE_BUDGET, words["more_budget"]),
               (HANDOFF, words["agent"])]
    facts = grounding.facts_for([], request=request, gazetteer=gazetteer,
                                extra_money=[lowest] if lowest else ())
    return RenderPlan([whatsapp.reply_buttons(body, buttons)], NO_MATCH_KIND, language,
                      facts=facts)


def voice_echo(request, language, gazetteer, version):
    """The R16 7.4 echo before a search (Task 10.2): the stored voice-critical values as
    the wrapper writes them, and [Haan] / [Badlein]. Backend values only (R-21)."""
    words = TEXT[language]
    facts = [" / ".join(_place_names(request, gazetteer)), _size_text(request, language),
             _budget_text(request, language).lstrip(", "),
             words.get(f"to_{request.purpose}", "")]
    body = words["voice_echo"].format(facts=", ".join(f for f in facts if f))
    payload = whatsapp.reply_buttons(body, [(ECHO_YES.format(version), words["echo_yes"]),
                                            (ECHO_EDIT, words["echo_edit"])])
    facts = grounding.facts_for([], request=request, gazetteer=gazetteer)
    found = grounding.check_payloads([payload], facts, gazetteer)
    if found:
        raise GroundingError(f"template stated unsupported facts: {found}")
    return RenderPlan([payload], CONFIRMATION, language, facts=facts)


def _candidates(pending):
    if pending is None:
        return None, []
    slot = getattr(pending, "slot", None) or (pending.get("slot") if isinstance(
        pending, dict) else None)
    found = getattr(pending, "candidates", None)
    if found is None and isinstance(pending, dict):
        found = pending.get("candidates")
    return slot, list(found or ())


def _clarification(payload, language, pending, gazetteer):
    """One question, no listings (SEARCH.md: the gate never shows stale results)."""
    words, missing = TEXT[language], list(payload.get("missing") or ())
    slot, candidates = _candidates(pending)
    places = [c["location_id"] for c in candidates if c.get("location_id") is not None]
    facts = grounding.facts_for([], gazetteer=gazetteer, extra_places=places)
    if slot == "location" and candidates:
        names = [c.get("name") or "?" for c in candidates][:whatsapp.LIST_ROWS_MAX]
        if len(names) == 1:
            payload_out = whatsapp.reply_buttons(
                words["confirm_place"].format(place=names[0]),
                [(CLARIFY_LOCATION.format(0), words["yes"]), (CLARIFY_NONE, words["no_place"])])
        elif len(names) <= whatsapp.MAX_BUTTONS and all(
                len(n) <= whatsapp.BUTTON_TITLE_MAX for n in names):
            payload_out = whatsapp.reply_buttons(
                words["which_place"],
                [(CLARIFY_LOCATION.format(i), n) for i, n in enumerate(names)])
        else:
            rows = [(CLARIFY_LOCATION.format(i), cards._fit(n, whatsapp.ROW_TITLE_MAX),
                     n if len(n) > whatsapp.ROW_TITLE_MAX else None)
                    for i, n in enumerate(names)]
            payload_out = whatsapp.list_message(words["which_place"], words["choose_place"],
                                                [(None, rows)])
    elif slot == "purpose" or "purpose" in missing:
        payload_out = whatsapp.reply_buttons(
            words["purpose_ask"], [(CLARIFY_PURPOSE.format("sale"), words["buy"]),
                                   (CLARIFY_PURPOSE.format("rent"), words["rent"])])
    elif "location" in missing:
        payload_out = whatsapp.text(words["location_ask"])
    elif "type_budget_or_size" in missing:
        payload_out = whatsapp.text(words["type_ask"])
    else:
        payload_out = whatsapp.text(words["generic_ask"])
    return RenderPlan([payload_out], CLARIFICATION, language, facts=facts)
