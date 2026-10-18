"""Search service (Task 8.1, component C9; R17 2.1, Red-Team R-24 / R-25).

Deterministic, no LLM: the stored requirement state (app.requirements, 7.3) becomes a
structured query over v_public_listing (ACTIVE only, RLS-scoped to the agency).

1. Gate (R17 2.1 step 4): purpose known (an inferred purpose passes and is echoed);
   location resolved (or the agency city); at least one of type / category, budget,
   size; no open pending_clarification. Otherwise clarification_needed + missing[].
2. Hard filters, never relaxed (step 5): agency (RLS), status ACTIVE (the view), purpose,
   property types (apartment includes penthouse) or the category's types, location =
   the union of the requested subtrees (the agency city for ANYWHERE_IN_CITY), and
   construction / possession / investment tags when the customer asked for them.
3. Tolerances (step 6): price <= max x 1.05 (above max = above_budget) and >= min x 0.9;
   size within +/-10 % of the nominal sq yd (house / plot / portion) or +/-15 % of the
   sq ft (other types); beds / baths >= the minimum.
4. Ladder (step 7), only while fewer than 3 results, cumulative, stop at >= 3:
   budget +10 % (skipped after a relative "cheaper": no_budget_relaxation, Demo Scope
   3b rule) -> size +/-20 % -> neighbours (location_neighbor, both directions) ->
   beds - 1. Every relaxed result carries the reason(s) it needed.
5. Ranking (step 8): in-budget before above-budget, then exact before relaxed (owner
   decision 2026-10-13: the budget rule wins when they conflict), then the R17 score
   (location 30, price 25, size 15, soft tags 15, freshness 10, media 5), then
   last_confirmed_at (newer first), then listing code. Already-shown listings of the
   latest shown search (24 h) are demoted below the unshown ones unless they rank top 3;
   excluded_property_ids (the cheaper rule) never come back.
6. Assertion (R-24): every result is re-checked in code against the hard filters and
   the scope / price cap of its own ladder step; a violation is dropped and logged.
7. Payload (R17 2.1 output): top 5 with public facts and labels, total_matches,
   relaxations_applied, excluded_reasons counts, unmapped_prefs, echo, and on a no-match
   the lowest available price in the requested scope (R17 edge 8). One search_run row
   records what was searched; shown_item rows are written by record_shown() after the
   results were actually sent (Task 10.1), so a failed send leaves no display history.

Never logs listing facts, requirement values or message text.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from psycopg.types.json import Jsonb

from app import location, normalise
from app.db import tenant_transaction

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "5s"
SHOWN_WINDOW = "24 hours"  # positions of the latest shown search stay valid 24 h (R17 5)

# R17 2.1 steps 6-7.
PRICE_ABOVE = Decimal("1.05")
PRICE_BELOW = Decimal("0.90")
SIZE_TOLERANCE_SQYD = Decimal("0.10")
SIZE_TOLERANCE_SQFT = Decimal("0.15")
SIZE_RELAXED = Decimal("0.20")
BUDGET_STEP = Decimal("1.10")
MIN_RESULTS = 3
TOP = 5
LADDER = ("budget", "size", "neighbour", "beds")

# R17 2.1 step 8.
WEIGHTS = {"location": 30, "price": 25, "size": 15, "tags": 15, "freshness": 10, "media": 5}
LOCATION_SCORE = {"exact": 1.0, "descendant": 0.9, "neighbour": 0.6}
FRESH_DAYS = 45      # docs/DEMO_DATASET.md freshness policy: re-confirm by 45 days
MEDIA_FULL = 3       # "Media completeness (>= 3 photos)"

TYPE_FAMILY = {"apartment": ("apartment", "penthouse")}
CONSTRUCTION = frozenset({"ready", "under_construction", "grey_structure", "off_plan"})
POSSESSION = frozenset({"possession", "on_ground", "file", "balloted", "allotted"})
INVESTMENT_TAGS = frozenset({"rental_income", "capital_gain", "pre_launch", "installments"})

OK, NO_MATCH, CLARIFY = "ok", "no_match", "clarification_needed"


def _set_timeout(conn):
    conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")


def _value(slots, name):
    entry = slots.get(name)
    return entry.get("value") if isinstance(entry, dict) else None


# --- request ------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    purpose: str | None
    types: tuple = ()              # property types allowed (hard filter); () = any
    nodes: tuple = ()              # requested location ids
    mode: str | None = None        # EXACT / SUBTREE / ANYWHERE_IN_CITY
    budget_min: int | None = None
    budget_max: int | None = None
    stated: int | None = None
    no_budget_relaxation: bool = False
    size: dict | None = None
    beds_min: int | None = None
    baths_min: int | None = None
    construction: tuple = ()
    possession: tuple = ()
    investment_tags: tuple = ()
    excluded: frozenset = frozenset()
    unmapped: tuple = ()
    echo: tuple = ()               # ((slot, (reasons...)), ...) for unconfirmed slots

    @classmethod
    def from_slots(cls, slots):
        types = list(_value(slots, "property_types") or [])
        category = _value(slots, "category")
        if not types and category:
            types = sorted(normalise.RESIDENTIAL if category == "residential"
                           else normalise.COMMERCIAL)
        allowed = []
        for kind in types:
            for member in TYPE_FAMILY.get(kind, (kind,)):
                if member not in allowed:
                    allowed.append(member)
        place = _value(slots, "location") or {}
        budget = _value(slots, "budget") or {}
        investment = _value(slots, "investment") or {}
        echo = tuple(sorted(
            (name, tuple(entry.get("echo") or ()))
            for name, entry in slots.items()
            if isinstance(entry, dict) and not entry.get("confirmed", True)
            and entry.get("echo")))
        return cls(
            purpose=_value(slots, "purpose"),
            types=tuple(allowed),
            nodes=tuple(str(n) for n in place.get("nodes") or ()),
            mode=place.get("mode"),
            budget_min=budget.get("min"),
            budget_max=budget.get("max"),
            stated=budget.get("stated_pkr"),
            no_budget_relaxation=bool(budget.get("no_budget_relaxation")),
            size=_value(slots, "size") or None,
            beds_min=_value(slots, "beds_min"),
            baths_min=_value(slots, "baths_min"),
            construction=tuple(v for v in _value(slots, "construction") or ()
                               if v in CONSTRUCTION),
            possession=tuple(v for v in _value(slots, "possession") or () if v in POSSESSION),
            investment_tags=tuple(v for v in (investment.get("tags") or ())
                                  if v in INVESTMENT_TAGS) if isinstance(investment, dict)
            else (),
            excluded=frozenset(str(i) for i in _value(slots, "excluded_property_ids") or ()),
            unmapped=tuple(p.get("raw") for p in _value(slots, "unmapped_prefs") or ()
                           if isinstance(p, dict) and p.get("raw")),
            echo=echo,
        )

    def filters(self):
        """The customer-visible criteria for search_run (docs/STATE_SCHEMA.md)."""
        return {
            "purpose": self.purpose, "types": list(self.types), "nodes": list(self.nodes),
            "mode": self.mode, "budget_min": self.budget_min, "budget_max": self.budget_max,
            "no_budget_relaxation": self.no_budget_relaxation,
            "size": {k: (self.size or {}).get(k) for k in
                     ("nominal_sqyd", "sqft", "mode", "min_sqft", "max_sqft")}
            if self.size else None,
            "beds_min": self.beds_min, "baths_min": self.baths_min,
            "construction": list(self.construction), "possession": list(self.possession),
            "investment_tags": list(self.investment_tags), "excluded": len(self.excluded),
        }


def gate(slots, pending_open=False):
    """The reasons a search cannot run yet (R17 2.1 step 4); [] = it can."""
    missing = []
    if pending_open:
        missing.append("pending_clarification")
    if not _value(slots, "purpose"):
        missing.append("purpose")
    place = _value(slots, "location") or {}
    if place.get("status", "RESOLVED") != "RESOLVED" or not place.get("nodes"):
        missing.append("location")
    budget = _value(slots, "budget") or {}
    has_budget = budget.get("min") is not None or budget.get("max") is not None
    if not (_value(slots, "property_types") or _value(slots, "category") or has_budget
            or _value(slots, "size")):
        missing.append("type_budget_or_size")
    return missing


# --- tolerances (pure) --------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    """One v_public_listing row (public columns only) and its photo count."""
    id: str
    listing_code: str
    status: str
    purpose: str
    property_type: str
    category: str | None
    location_id: str
    price_pkr: int
    area_value: object
    area_unit: str | None
    area_sqft: object
    size_nominal_sqyd: int | None
    bedrooms: int | None
    bathrooms: int | None
    construction_status: str | None
    possession_status: str | None
    investment_tags: tuple
    last_confirmed_at: object
    photos: int = 0

    @property
    def sqyd(self):
        if self.size_nominal_sqyd:
            return Decimal(self.size_nominal_sqyd)
        return Decimal(str(self.area_sqft)) / 9 if self.area_sqft is not None else None


def price_cap(request, relaxed):
    if request.budget_max is None:
        return None
    step = BUDGET_STEP if relaxed else Decimal(1)
    return Decimal(request.budget_max) * step * PRICE_ABOVE


def price_floor(request):
    return None if request.budget_min is None else Decimal(request.budget_min) * PRICE_BELOW


def price_fits(row, request, relaxed=False):
    cap, floor = price_cap(request, relaxed), price_floor(request)
    return (cap is None or row.price_pkr <= cap) and (floor is None or row.price_pkr >= floor)


def size_fits(row, request, relaxed=False):
    size = request.size
    if not size:
        return True
    if row.property_type in normalise.SQYD_TYPES:
        have = row.sqyd
        tolerance = SIZE_TOLERANCE_SQYD
        target = size.get("nominal_sqyd") or (Decimal(size["sqft"]) / 9 if size.get("sqft")
                                              else None)
        low = Decimal(size["min_sqft"]) / 9 if size.get("min_sqft") else None
        high = Decimal(size["max_sqft"]) / 9 if size.get("max_sqft") else None
    else:
        have = Decimal(str(row.area_sqft)) if row.area_sqft is not None else None
        tolerance = SIZE_TOLERANCE_SQFT
        target = size.get("sqft")
        low, high = size.get("min_sqft"), size.get("max_sqft")
    if relaxed:
        tolerance = SIZE_RELAXED
    if have is None:
        return False
    mode = size.get("mode") or "target"
    if mode == "min":
        floor = low if low is not None else target
        return floor is None or have >= Decimal(floor) * (1 - tolerance)
    if mode == "range" and (low is not None or high is not None):
        return ((low is None or have >= Decimal(low) * (1 - tolerance))
                and (high is None or have <= Decimal(high) * (1 + tolerance)))
    if target is None:
        return True
    target = Decimal(target)
    return target * (1 - tolerance) <= have <= target * (1 + tolerance)


def beds_fit(row, request, relaxed=False):
    if not request.beds_min:
        return True
    need = request.beds_min - (1 if relaxed else 0)
    return row.bedrooms is not None and row.bedrooms >= need


def baths_fit(row, request):
    return not request.baths_min or (row.bathrooms is not None
                                     and row.bathrooms >= request.baths_min)


def above_budget(row, request):
    return request.budget_max is not None and row.price_pkr > request.budget_max


def labels(row, request, place):
    """The ladder steps this row needs (exact = []), or None if it fails even with
    every step relaxed. place: exact / descendant / neighbour."""
    needed = []
    if not price_fits(row, request):
        if request.no_budget_relaxation or not price_fits(row, request, relaxed=True):
            return None
        needed.append("budget")
    if not size_fits(row, request):
        if not size_fits(row, request, relaxed=True):
            return None
        needed.append("size")
    if place == "neighbour":
        needed.append("neighbour")
    if not beds_fit(row, request):
        if not beds_fit(row, request, relaxed=True):
            return None
        needed.append("beds")
    if not baths_fit(row, request):
        return None
    return needed


def first_miss(row, request):
    """Why a row in scope is not a result (excluded_reasons)."""
    if row.id in request.excluded:
        return "already_shown"
    if not price_fits(row, request):
        return "over_budget" if price_cap(request, False) is not None and \
            row.price_pkr > price_cap(request, False) else "under_budget"
    if not size_fits(row, request):
        return "size"
    if not beds_fit(row, request):
        return "beds"
    if not baths_fit(row, request):
        return "baths"
    return "relaxation_not_needed"


# --- scoring (pure) -----------------------------------------------------------------

def price_score(row, request):
    if request.budget_max is None and request.stated is None:
        return 1.0
    if request.stated and request.budget_min is not None and request.budget_max is not None:
        # around / range: closeness to the stated amount
        return max(0.0, 1 - abs(row.price_pkr - request.stated) / request.stated)
    top = request.budget_max or request.stated
    if row.price_pkr <= top:
        return row.price_pkr / top          # under budget and close to max is best
    return max(0.0, 0.5 - (row.price_pkr - top) / top * 5)  # above_budget penalised


def size_score(row, request):
    size = request.size
    if not size:
        return 1.0
    if row.property_type in normalise.SQYD_TYPES:
        have, target = row.sqyd, size.get("nominal_sqyd") or (
            Decimal(size["sqft"]) / 9 if size.get("sqft") else None)
    else:
        have = Decimal(str(row.area_sqft)) if row.area_sqft is not None else None
        target = size.get("sqft") or size.get("min_sqft")
    if have is None or not target:
        return 0.5
    return max(0.0, 1 - float(abs(have - Decimal(target)) / Decimal(target)))


def freshness_score(row, now):
    if row.last_confirmed_at is None or now is None:
        return 0.0
    days = (now - row.last_confirmed_at).total_seconds() / 86400
    return max(0.0, 1 - max(days, 0) / FRESH_DAYS)


def score(row, request, place, now):
    parts = {
        "location": LOCATION_SCORE[place],
        "price": price_score(row, request),
        "size": size_score(row, request),
        "tags": 0.0,  # no soft-preference vocabulary yet (docs/REQUIREMENTS.md)
        "freshness": freshness_score(row, now),
        "media": min(row.photos, MEDIA_FULL) / MEDIA_FULL,
    }
    return round(sum(WEIGHTS[k] * v for k, v in parts.items()), 4)


@dataclass
class Hit:
    row: Row
    place: str
    needed: list
    above_budget: bool
    score: float
    shown: bool = False

    @property
    def relaxed(self):
        return bool(self.needed)

    def sort_key(self):
        confirmed = self.row.last_confirmed_at.timestamp() if self.row.last_confirmed_at \
            else 0
        return (self.above_budget, self.relaxed, -self.score, -confirmed,
                self.row.listing_code)


def rank(hits):
    """In-budget > above-budget, exact > relaxed, score, freshness, code; then shown
    listings outside the top 3 move below the unshown ones (R17 2.1 step 8)."""
    ordered = sorted(hits, key=Hit.sort_key)
    head = ordered[:MIN_RESULTS]
    rest = ordered[MIN_RESULTS:]
    return head + [h for h in rest if not h.shown] + [h for h in rest if h.shown]


# --- the search ---------------------------------------------------------------------

@dataclass
class Result:
    status: str
    missing: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    total_matches: int = 0
    relaxations: list = field(default_factory=list)
    excluded_reasons: dict = field(default_factory=dict)
    unmapped_prefs: list = field(default_factory=list)
    echo: list = field(default_factory=list)
    lowest_price_pkr: int | None = None
    search_run_id: object = None
    dropped: int = 0
    no_neighbours: bool = False   # nearby was asked for, but none is configured
    request: object = None        # the Request this search ran on (what the reply must state)

    @property
    def codes(self):
        return [h.row.listing_code for h in self.hits]

    def payload(self, gazetteer=None):
        """What the model / renderer sees (R17 2.1 output): public facts only."""
        ids = {str(k): k for k in gazetteer.places} if gazetteer is not None else {}

        def place_name(location_id):
            place = ids.get(location_id)
            return gazetteer.places[place].display_name if place is not None else None
        return {
            "status": self.status,
            "missing": list(self.missing),
            "results": [{
                "position": n, "id": h.row.id, "listing_code": h.row.listing_code,
                "purpose": h.row.purpose, "property_type": h.row.property_type,
                "location": place_name(h.row.location_id), "price_pkr": h.row.price_pkr,
                "area_value": str(h.row.area_value) if h.row.area_value is not None else None,
                "area_unit": h.row.area_unit, "size_nominal_sqyd": h.row.size_nominal_sqyd,
                "bedrooms": h.row.bedrooms, "bathrooms": h.row.bathrooms,
                "above_budget": h.above_budget, "relaxations": list(h.needed),
                "location_match": h.place,
            } for n, h in enumerate(self.hits[:TOP], 1)],
            "total_matches": self.total_matches,
            "relaxations_applied": list(self.relaxations),
            "excluded_reasons": dict(self.excluded_reasons),
            "unmapped_prefs": list(self.unmapped_prefs),
            "echo": [{"slot": s, "reasons": list(r)} for s, r in self.echo],
            "lowest_price_pkr": self.lowest_price_pkr,
        }


_ROW_SQL = """
    SELECT v.id::text, v.listing_code, v.status, v.purpose, v.property_type, v.category,
           v.location_id::text, v.price_pkr, v.area_value, v.area_unit, v.area_sqft,
           v.size_nominal_sqyd, v.bedrooms, v.bathrooms, v.construction_status,
           v.possession_status, v.investment_tags, v.last_confirmed_at,
           (SELECT count(*) FROM property_media pm WHERE pm.property_id = v.id)
    FROM v_public_listing v
    WHERE v.purpose = %s AND v.location_id::text = ANY(%s)
"""


def fetch_rows(conn, request, scope):
    """Hard filters only (R17 2.1 step 5); tolerances are applied in code."""
    sql, params = _ROW_SQL, [request.purpose, sorted(scope)]
    if request.types:
        sql += " AND v.property_type = ANY(%s)"
        params.append(list(request.types))
    if request.construction:
        sql += " AND v.construction_status = ANY(%s)"
        params.append(list(request.construction))
    if request.possession:
        sql += " AND v.possession_status = ANY(%s)"
        params.append(list(request.possession))
    if request.investment_tags:
        sql += " AND v.investment_tags && %s::text[]"
        params.append(list(request.investment_tags))
    sql += " ORDER BY v.listing_code"
    return [Row(*r[:16], tuple(r[16] or ()), r[17], int(r[18]))
            for r in conn.execute(sql, params).fetchall()]


def subtree(gazetteer, nodes):
    """str ids of every place within these nodes (gazetteer ids)."""
    return {str(p) for p in gazetteer.places if any(gazetteer.is_within(p, n) for n in nodes)}


def neighbours(conn, gazetteer, nodes):
    """Curated neighbours (location_neighbor, stored once per pair) of the requested
    nodes and of their sub-area ancestors (a zone inside Phase 6 has Phase 6's)."""
    seeds = set()
    for node in nodes:
        for ancestor in gazetteer.ancestors(node):
            if gazetteer.places[ancestor].level in ("city", "area"):
                break
            seeds.add(ancestor)
    if not seeds:
        return set()
    rows = conn.execute(
        """
        SELECT location_b_id::text FROM location_neighbor WHERE location_a_id::text = ANY(%s)
        UNION
        SELECT location_a_id::text FROM location_neighbor WHERE location_b_id::text = ANY(%s)
        """,
        (sorted(map(str, seeds)), sorted(map(str, seeds))),
    ).fetchall()
    ids = {str(k): k for k in gazetteer.places}
    return {ids[r[0]] for r in rows if r[0] in ids}


def shown_ids(conn, conversation_id):
    """Property ids of the latest search whose results were shown (24 h)."""
    rows = conn.execute(
        f"""
        SELECT si.property_id::text FROM shown_item si
        WHERE si.search_run_id = (
            SELECT r.id FROM search_run r
            WHERE r.conversation_id = %s AND r.created_at > now() - interval '{SHOWN_WINDOW}'
              AND EXISTS (SELECT 1 FROM shown_item x WHERE x.search_run_id = r.id)
            ORDER BY r.created_at DESC LIMIT 1)
        """,
        (conversation_id,),
    ).fetchall()
    return {r[0] for r in rows}


def check(hit, request, requested, near, city):
    """R-24: re-check a result against the hard filters and its step; a reason or None."""
    row = hit.row
    if row.status != "active":
        return "status"
    if row.purpose != request.purpose:
        return "purpose"
    if request.types and row.property_type not in request.types:
        return "type"
    if row.location_id not in city:
        return "city"
    allowed = requested | (near if "neighbour" in hit.needed else set())
    if row.location_id not in allowed:
        return "location"
    if row.id in request.excluded:
        return "excluded"
    cap = price_cap(request, "budget" in hit.needed)
    if cap is not None and row.price_pkr > cap:
        return "price"
    if request.construction and row.construction_status not in request.construction:
        return "construction"
    return None


def run(conn, gazetteer, *, conversation_id, requirement_version, slots,
        pending_open=False, city_code="KHI", nearby=False):
    """One search for a conversation's stored requirements, inside the caller's tenant
    transaction. Writes one search_run unless the gate stops it. nearby (the no-match
    "Nearby area" tap, Task 10.1): the curated neighbours are searched from the start,
    next to the requested place and with every other requirement kept; with none
    configured nothing is searched (no_neighbours)."""
    missing = gate(slots, pending_open)
    request = Request.from_slots(slots)
    if missing:
        return Result(CLARIFY, missing=missing, unmapped_prefs=list(request.unmapped),
                      echo=list(request.echo), request=request)
    ids = {str(k): k for k in gazetteer.places}  # UUIDs in the database, keys in tests
    city_node = gazetteer.city_node(city_code)
    city = subtree(gazetteer, [city_node]) if city_node is not None else set()
    nodes = [ids[n] for n in request.nodes if n in ids]
    node_ids = {str(n) for n in nodes}
    if not nodes:
        return Result(CLARIFY, missing=["location"])
    if request.mode == location.ANYWHERE_IN_CITY or city_node in nodes:
        requested, near = set(city), set()
    else:
        requested = subtree(gazetteer, nodes) & city
        near = (subtree(gazetteer, neighbours(conn, gazetteer, nodes)) & city) - requested
    if nearby and not near:
        return Result(NO_MATCH, no_neighbours=True)
    now = conn.execute("SELECT now()").fetchone()[0]
    rows = fetch_rows(conn, request, requested | near)
    shown = shown_ids(conn, conversation_id)

    def place_of(row):
        if row.location_id in requested:
            exact = request.mode == location.ANYWHERE_IN_CITY or row.location_id in node_ids
            return "exact" if exact else "descendant"
        return "neighbour"

    candidates = []
    for row in rows:
        if row.id in request.excluded:
            continue
        place = place_of(row)
        needed = labels(row, request, place)
        if needed is not None:
            candidates.append(Hit(row, place, needed, above_budget(row, request),
                                  score(row, request, place, now), row.id in shown))

    applicable = {
        "budget": request.budget_max is not None and not request.no_budget_relaxation,
        "size": bool(request.size),
        "neighbour": bool(near),
        "beds": bool(request.beds_min),
    }
    enabled = {"neighbour"} if nearby else set()
    hits = [h for h in candidates if set(h.needed) <= enabled]
    for step in LADDER:
        if len(hits) >= MIN_RESULTS:
            break
        if not applicable[step]:
            continue
        enabled.add(step)
        hits = [h for h in candidates if set(h.needed) <= enabled]

    kept = []
    for hit in rank(hits):
        reason = check(hit, request, requested, near, city)
        if reason is None:
            kept.append(hit)
        else:
            logger.error("Search assertion dropped listing %s (%s)", hit.row.id, reason)
    dropped = len(hits) - len(kept)

    in_play = {h.row.id for h in kept}
    reasons = {}
    for row in rows:
        if row.id in in_play or (row.location_id in near and "neighbour" not in enabled):
            continue
        miss = first_miss(row, request)
        if miss != "relaxation_not_needed":
            reasons[miss] = reasons.get(miss, 0) + 1
    relaxations = [s for s in LADDER if any(s in h.needed for h in kept)]
    lowest = None
    if not kept:
        prices = [r.price_pkr for r in rows
                  if r.location_id in requested and r.id not in request.excluded]
        lowest = min(prices) if prices else None

    result = Result(OK if kept else NO_MATCH, hits=kept, total_matches=len(kept),
                    relaxations=relaxations, excluded_reasons=reasons,
                    unmapped_prefs=list(request.unmapped), echo=list(request.echo),
                    lowest_price_pkr=lowest, dropped=dropped, request=request)
    result.search_run_id = conn.execute(
        """
        INSERT INTO search_run (agency_id, conversation_id, requirement_version, filters,
                                relaxations, total_matches)
        VALUES (current_agency_id(), %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (conversation_id, requirement_version, Jsonb(request.filters()),
         Jsonb(relaxations), len(kept)),
    ).fetchone()[0]
    logger.info("Search %s: %s, %d result(s), relaxations [%s]", result.search_run_id,
                result.status, len(kept), ", ".join(relaxations))
    return result


def search_conversation(conn, agency_id, conversation_id, gazetteer, nearby=False):
    """run() for a conversation's stored state in its own tenant transaction."""
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        state = conn.execute(
            "SELECT version, slots FROM requirement_state WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()
        version, slots = state if state else (0, {})
        pending = conn.execute(
            "SELECT 1 FROM pending_clarification WHERE conversation_id = %s "
            "AND status = 'open'", (conversation_id,),
        ).fetchone() is not None
        city = conn.execute(
            "SELECT city_default FROM agency WHERE id = current_agency_id()"
        ).fetchone()
        if version == 0:
            return Result(CLARIFY, missing=gate({}, pending))
        return run(conn, gazetteer, conversation_id=conversation_id,
                   requirement_version=version, slots=slots, pending_open=pending,
                   city_code=(city[0] if city and city[0] else "KHI"), nearby=nearby)


def record_shown(conn, agency_id, conversation_id, search_run_id, property_ids,
                 card_message_ids=None):
    """After the results were sent (Task 10.1): shown_item rows in position order. Only
    shown listings count for "Property 2", demotion and the cheaper rule."""
    cards = list(card_message_ids or [None] * len(property_ids))
    with tenant_transaction(conn, agency_id):
        _set_timeout(conn)
        for position, (property_id, card) in enumerate(zip(property_ids, cards), 1):
            conn.execute(
                """
                INSERT INTO shown_item (agency_id, conversation_id, search_run_id, position,
                                        property_id, card_message_id)
                VALUES (current_agency_id(), %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (conversation_id, search_run_id, position, property_id, card),
            )
