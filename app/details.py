"""Property detail answers and the unknown path (Task 9.1; Red-Team R-05, R-26, CLAUDE.md
rule 1; docs/RENDERER.md).

A customer question about one listing ("gas connection hai?", "parking hai?",
"documents clear hain?") is answered only from that listing's public row, re-fetched
at answer time (load_known, v_public_listing, R-26):

- a known field -> a template line built from the value;
- an absent field -> "Ye maloomat mere paas nahi -- agent confirm kar denge." + an
  agent button. Absence is unknown, never false (R-05);
- legal / document questions -> always the agent, even when a related field exists;
- price negotiation -> always the agent (no AI negotiation, CLAUDE.md section 10).

field_for maps question text to fields deterministically, so 10.1 can answer without the
model choosing a value. Pure except load_known; no model call.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from app import whatsapp
from app.cards import ENGLISH, LANGUAGES, ROMAN_URDU, price_text
from app.db import tenant_transaction

STATEMENT_TIMEOUT = "5s"
LEGAL, NEGOTIATION = "legal", "negotiation"
AGENT_BUTTON = "handoff:request"  # the same tap as the no-match "Agent se baat"

# The 007 feature enum -> (English, Roman Urdu) "it has ..." phrases.
FEATURES = {
    "basement": ("has a basement", "basement hai"),
    "annex": ("has an annexe", "annexe hai"),
    "servant_quarter": ("has a servant quarter", "servant quarter hai"),
    "lift": ("has a lift", "lift hai"),
    "generator_backup": ("has generator backup", "generator backup hai"),
    "solar": ("has solar power", "solar hai"),
    "gas_connection": ("has a gas connection", "gas connection hai"),
    "water_kwsb": ("has a KWSB water connection", "KWSB pani ka connection hai"),
    "water_boring": ("has a water boring", "pani ki boring hai"),
    "security": ("has security", "security hai"),
    "lawn": ("has a lawn", "lawn hai"),
    "roof_access": ("has roof access", "roof access hai"),
    "sea_view": ("has a sea view", "sea view hai"),
    "main_boulevard": ("is on the main boulevard", "main boulevard par hai"),
    "drawing_dining": ("has a drawing / dining room", "drawing / dining hai"),
}
FACING = {"west_open": "west open", "east_open": "east open", "north_open": "north open",
          "south_open": "south open", "park_facing": "park facing",
          "road_facing": "road facing", "sea_facing": "sea facing"}
FURNISHING = {"furnished": ("is furnished", "furnished hai"),
              "semi": ("is semi-furnished", "semi-furnished hai"),
              "unfurnished": ("is not furnished", "furnished nahi hai")}
POSSESSION = {"possession": "possession", "on_ground": "on ground", "file": "file",
              "balloted": "balloted", "allotted": "allotted"}
CONSTRUCTION = {"ready": ("ready", "tayyar"),
                "under_construction": ("under construction", "zer-e-tameer"),
                "grey_structure": ("grey structure", "grey structure"),
                "off_plan": ("off-plan", "off-plan")}

# Question text -> fields; first match wins per span, legal and negotiation first.
FIELD_PATTERNS = tuple((name, re.compile(p, re.IGNORECASE)) for name, p in (
    (LEGAL, (r"\b(?:documents?|docs|kag(?:h)?zat|papers?|noc|registr\w*|lease\w*|legal\w*|"
             r"transfer|mutation|ownership|file\s+clear|clear\s+title|approv\w*|"
             r"qanooni|disputed?)\b")),
    (NEGOTIATION, (r"\b(?:negotia\w*|discount|(?:price|rate|qeemat)\s+kam|kam\s+(?:ho|kar)\w*|"
                   r"last\s+price|final\s+price|best\s+price)\b")),
    ("security_deposit", r"\b(?:security\s+)?deposit\b"),
    ("advance", r"\badvance\b"),
    ("maintenance", r"\bmaintenance\b"),
    ("gas_connection", r"\b(?:sui\s+)?gas\b"),
    ("water", r"\b(?:water|pani|paani|boring|kwsb)\b"),
    ("lift", r"\b(?:lifts?|elevators?)\b"),
    ("parking", r"\b(?:parking|garage|car\s*porch)\b"),
    ("furnishing", r"\b(?:furnish\w*|furniture)\b"),
    ("corner", r"\bcorner\b"),
    ("facing", r"\b(?:facing|(?:west|east|north|south)\s+open)\b"),
    ("possession", r"\b(?:possession|qabz[ae]|handover|kab\s+mil\w*)\b"),
    ("construction", r"\b(?:construction|tayyar|ready|grey\s+structure|off-?plan)\b"),
    ("floor", r"\b(?:floors?|manzil)\b"),
    ("available_from", r"\b(?:available\s+from|kab\s+se|move\s+in|shift)\b"),
    ("installments", r"\b(?:installments?|instalments?|qist\w*|aqsaat|down\s*payment)\b"),
    ("tenanted", r"\b(?:tenant\w*|kirayedar|kiraydar)\b"),
    ("generator_backup", r"\b(?:generator|backup|ups)\b"),
    ("solar", r"\bsolar\b"),
    ("security", r"\b(?:security|guards?|gated)\b"),
    ("lawn", r"\b(?:lawn|garden)\b"),
    ("basement", r"\b(?:basement|tehkhana)\b"),
    ("annex", r"\bannexe?\b"),
    ("servant_quarter", r"\bservant\b"),
    ("roof_access", r"\b(?:roof|chhat)\b"),
    ("sea_view", r"\b(?:sea|samandar|samundar)\b"),
    ("main_boulevard", r"\bboulevard\b"),
    ("drawing_dining", r"\b(?:drawing|dining)\b"),
    ("bedrooms", r"\b(?:bed(?:room)?s?|kamr[ae]y?)\b"),
    ("bathrooms", r"\b(?:bath(?:room)?s?|washrooms?|toilets?)\b"),
    ("nearby_places", r"\b(?:school|hospital|masjid|mosque|market|mall|qareeb\s+kya)\b"),
))

TEXT = {
    ENGLISH: {
        "fact": "{ref}: {fact}.",
        "unknown": "I don't have that information -- the agent will confirm it.",
        "legal": ("Documents and legal questions are always confirmed by the agent -- I can "
                  "connect you."),
        "negotiation": "Price discussions are handled by the agent -- I can connect you.",
        "agent": "Talk to agent",
        "parking": "{n} parking space(s)", "floor": "on floor {n}",
        "floors": "{n} floors in the building", "maintenance": "maintenance {amount} a month",
        "deposit": "security deposit {amount}", "advance": "{n} month(s) advance",
        "available": "available from {when}", "installments": "installments available",
        "no_installments": "no installment plan", "down_payment": "down payment {amount}",
        "tenanted": "currently tenanted", "vacant": "not tenanted",
        "corner": "is a corner property", "not_corner": "is not a corner property",
        "facing": "is {facing}", "possession": "possession status: {status}",
        "expected": "expected possession {when}", "construction": "is {status}",
        "water": "{water}", "beds": "{n} bedroom(s)", "baths": "{n} bathroom(s)",
    },
    ROMAN_URDU: {
        "fact": "{ref}: {fact}.",
        "unknown": "Ye maloomat mere paas nahi -- agent confirm kar denge.",
        "legal": ("Documents aur legal baatein hamesha agent hi confirm karte hain -- main aap "
                  "ko agent se mila sakta hoon."),
        "negotiation": "Price ki baat agent hi karte hain -- main aap ko agent se mila sakta hoon.",
        "agent": "Agent se baat",
        "parking": "{n} parking hain", "floor": "{n} floor par hai",
        "floors": "building mein {n} floors hain", "maintenance": "maintenance {amount} mahana",
        "deposit": "security deposit {amount}", "advance": "{n} mahine ka advance",
        "available": "{when} se available", "installments": "qiston par available hai",
        "no_installments": "qiston ka plan nahi hai", "down_payment": "down payment {amount}",
        "tenanted": "abhi kirayedar maujood hai", "vacant": "kirayedar nahi hai",
        "corner": "corner hai", "not_corner": "corner nahi hai",
        "facing": "{facing} hai", "possession": "possession status: {status}",
        "expected": "possession {when} tak expected hai", "construction": "{status} hai",
        "water": "{water}", "beds": "{n} bedroom", "baths": "{n} bathroom",
    },
}


@dataclass(frozen=True)
class KnownListing:
    """One listing's public facts (v_public_listing columns + the allowed public_features
    keys). None / absent = unknown."""
    code: str
    purpose: str | None = None
    property_type: str | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    features: tuple = ()
    facing: tuple = ()
    is_corner: bool | None = None
    furnishing: str | None = None
    possession_status: str | None = None
    expected_possession: date | None = None
    construction_status: str | None = None
    installment_available: bool | None = None
    is_tenanted: bool | None = None
    public: dict = field(default_factory=dict, compare=False)


def load_known(conn, agency_id, code):
    """The listing's public facts now (R-26), or None when it is not an active public
    listing of this agency (RLS + v_public_listing)."""
    with tenant_transaction(conn, agency_id):
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        row = conn.execute(
            """
            SELECT listing_code, purpose, property_type, bedrooms, bathrooms, features,
                   facing, is_corner, furnishing, possession_status, expected_possession,
                   construction_status, installment_available, is_tenanted, public_features
            FROM v_public_listing WHERE listing_code = %s
            """,
            (code,),
        ).fetchone()
    if row is None:
        return None
    return KnownListing(*row[:5], tuple(row[5] or ()), tuple(row[6] or ()), *row[7:14],
                        public=dict(row[14] or {}))


def field_for(text):
    """The fields a question asks about, in order (legal / negotiation first); [] when
    no field is recognised (the caller then uses the unknown path)."""
    found, taken = [], []
    for name, pattern in FIELD_PATTERNS:
        for match in pattern.finditer(text or ""):
            if any(s < match.end() and match.start() < e for s, e in taken):
                continue
            taken.append(match.span())
            if name not in found:
                found.append(name)
    return sorted(found, key=lambda n: (n not in (LEGAL, NEGOTIATION),))


def _amount(value):
    return price_text(int(value), None) if value is not None else None


def known_fields(known, language=ENGLISH):
    """field -> the fact phrase, for the fields this row knows. Absent = unknown."""
    words, i = TEXT[language], (0 if language == ENGLISH else 1)
    out = {name: phrases[i] for name, phrases in FEATURES.items() if name in known.features}
    pub = known.public
    if known.is_corner is not None:
        out["corner"] = words["corner" if known.is_corner else "not_corner"]
    if known.facing:
        out["facing"] = words["facing"].format(
            facing=" / ".join(FACING.get(f, f) for f in known.facing))
    if known.furnishing in FURNISHING:
        out["furnishing"] = FURNISHING[known.furnishing][i]
    if known.possession_status in POSSESSION:
        out["possession"] = words["possession"].format(
            status=POSSESSION[known.possession_status])
    elif known.expected_possession is not None:
        out["possession"] = words["expected"].format(
            when=known.expected_possession.strftime("%B %Y"))
    if known.construction_status in CONSTRUCTION:
        out["construction"] = words["construction"].format(
            status=CONSTRUCTION[known.construction_status][i])
    water = [FEATURES[f][i] for f in ("water_kwsb", "water_boring") if f in known.features]
    if water:
        out["water"] = words["water"].format(water=", ".join(water))
    if pub.get("parking_spaces") is not None:
        out["parking"] = words["parking"].format(n=pub["parking_spaces"])
    floors = [words["floor"].format(n=pub["floor_no"])] if pub.get("floor_no") is not None \
        else []
    if pub.get("total_floors") is not None:
        floors.append(words["floors"].format(n=pub["total_floors"]))
    if floors:
        out["floor"] = ", ".join(floors)
    if pub.get("maintenance_pkr_month") is not None:
        out["maintenance"] = words["maintenance"].format(
            amount=_amount(pub["maintenance_pkr_month"]))
    if pub.get("rent_security_deposit_pkr") is not None:
        out["security_deposit"] = words["deposit"].format(
            amount=_amount(pub["rent_security_deposit_pkr"]))
    if pub.get("rent_advance_months") is not None:
        out["advance"] = words["advance"].format(n=pub["rent_advance_months"])
    if pub.get("available_from"):
        out["available_from"] = words["available"].format(when=pub["available_from"])
    if known.installment_available is not None:
        parts = [words["installments" if known.installment_available else "no_installments"]]
        if known.installment_available and pub.get("down_payment_pkr") is not None:
            parts.append(words["down_payment"].format(amount=_amount(pub["down_payment_pkr"])))
        out["installments"] = ", ".join(parts)
    if known.is_tenanted is not None:
        out["tenanted"] = words["tenanted" if known.is_tenanted else "vacant"]
    if known.bedrooms is not None:
        out["bedrooms"] = words["beds"].format(n=known.bedrooms)
    if known.bathrooms is not None:
        out["bathrooms"] = words["baths"].format(n=known.bathrooms)
    return out


def answer_detail(known, fields, language, position=None):
    """The reply payloads for a question about `fields` (field_for) of one listing (a
    KnownListing from load_known; None = no longer public). Facts for known fields; the
    agent template for legal, negotiation, unknown or a listing that is gone."""
    if language not in LANGUAGES:
        raise ValueError(f"unknown language {language!r}")
    words = TEXT[language]
    fields = [fields] if isinstance(fields, str) else list(fields)
    agent = [(AGENT_BUTTON, words["agent"])]
    if LEGAL in fields:
        return [whatsapp.reply_buttons(words["legal"], agent)]
    if NEGOTIATION in fields:
        return [whatsapp.reply_buttons(words["negotiation"], agent)]
    if known is None or not fields:
        return [whatsapp.reply_buttons(words["unknown"], agent)]
    facts = known_fields(known, language)
    ref = f"#{position} · {known.code}" if position is not None else known.code  # D3 echo
    lines = [words["fact"].format(ref=ref, fact=facts[f]) for f in fields if f in facts]
    if len(lines) == len(fields):
        return [whatsapp.text("\n".join(lines))]
    # Some (or all) asked fields are unknown: say what is known, then the agent path.
    return [whatsapp.reply_buttons("\n".join(lines + [words["unknown"]]), agent)]
