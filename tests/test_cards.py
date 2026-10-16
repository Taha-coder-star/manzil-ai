"""Task 5.2: result cards (carousel, single card, individual cards, overflow list), the
per-contact fallback flag (migration 011), the staff '#demo' card commands and the
card-tap stub.

Renderer tests are offline. Database tests seed the Task 4.3 dataset into a
disposable schema and use the restricted manzil_app login (RLS on); covers are
synthetic rows with fictional URLs. The WhatsApp client is a fake: nothing reaches
Meta, S3 or AWS.
"""

import csv
import threading
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace

import psycopg
import pytest

from app import cards, echo, handoff_actions, seed, webhook, whatsapp
from app import demo_dataset as dd
from app.db import tenant_transaction
from app.handlers import make_handlers
from app.ingest import store_event
from app.provision_agency import create_agency
from app.worker import Worker
from tests.test_gateway import FakeClient
from tests.test_seed import ENV as SEED_ENV

# Task 11.2: what a broker gets for anything that is not a lead command (R-15).
BROKER_HELP = handoff_actions.TEXT["help"] + "\n\n" + handoff_actions.TEXT["none_open"]

CDN = "https://demo-bucket.s3.us-east-1.amazonaws.com/card"


def listing(code="DH6-101", cover=True, **fields):
    values = {
        "code": code, "purpose": "sale", "property_type": "house", "price_pkr": 78_000_000,
        "area_value": Decimal(500), "area_unit": "sq_yd", "size_nominal_sqyd": Decimal(500),
        "bedrooms": 4, "bathrooms": 5, "construction_status": "ready",
        "location": "DHA Phase 6", "cover_url": f"{CDN}/{code}.jpg" if cover else None,
    }
    return cards.Listing(**{**values, **fields})


def many(count, covered=True):
    return [listing(f"DH6-{101 + n}", cover=covered) for n in range(count)]


def kinds(payloads):
    return [p["interactive"]["type"] if p["type"] == "interactive" else p["type"]
            for p in payloads]


def button_ids(payload):
    return [b["reply"]["id"] for b in payload["interactive"]["action"]["buttons"]]


def body(payload):
    return whatsapp.summary(payload)


# --- text ----------------------------------------------------------------------------

@pytest.mark.parametrize(("price", "purpose", "expected"), [
    (78_000_000, "sale", "PKR 7.8 crore"),
    (80_000_000, "sale", "PKR 8 crore"),
    (9_500_000, "sale", "PKR 95 lakh"),
    (450_000, "rent", "PKR 4.5 lakh / month"),
    (95_000, "rent", "PKR 95,000 / month"),
    (None, "sale", None),
])
def test_price_text(price, purpose, expected):
    assert cards.price_text(price, purpose) == expected


def test_area_rooms_and_status_text():
    bahria = listing(area_value=Decimal("272.00"), size_nominal_sqyd=Decimal(250))
    assert cards.area_text(bahria) == "272 sq yd (250 nominal)"
    shop = listing("PRJ-103", property_type="shop", area_value=Decimal(300),
                   area_unit="sq_ft", size_nominal_sqyd=None, bedrooms=None, bathrooms=None,
                   construction_status="off_plan")
    assert cards.area_text(shop) == "300 sq ft"
    assert cards.rooms_text(shop) is None and cards.rooms_text(listing()) == "4 bed / 5 bath"
    assert cards.status_text(shop) == "Off-plan" and cards.kind_text(shop) == "Shop for sale"
    assert cards.card_body(2, shop) == "#2 PRJ-103 · Shop for sale\nDHA Phase 6 · 300 sq ft\n" \
        "PKR 7.8 crore · Off-plan"


# --- formats --------------------------------------------------------------------------

def test_no_results_is_a_plain_text():
    assert kinds(cards.render([])) == ["text"]


def test_single_result_is_one_image_card_with_three_buttons():
    [payload] = cards.render([listing()])
    assert kinds([payload]) == ["button"]
    assert payload["interactive"]["header"] == {
        "type": "image", "image": {"link": f"{CDN}/DH6-101.jpg"},
    }
    assert button_ids(payload) == ["photos:DH6-101", "visit:DH6-101", "similar:DH6-101"]
    assert body(payload) == ("*DH6-101* · House for sale\nDHA Phase 6\n"
                             "500 sq yd · 4 bed / 5 bath\nPKR 7.8 crore · Ready")


def test_single_result_without_a_cover_gets_a_text_header():
    [payload] = cards.render([listing(cover=False)])
    assert payload["interactive"]["header"] == {
        "type": "text", "text": "DH6-101 · photos coming soon",
    }
    assert body(payload).endswith("(photos coming soon)")
    assert len(button_ids(payload)) == 3


@pytest.mark.parametrize("count", [2, 5])
def test_two_to_five_results_are_one_carousel(count):
    [payload] = cards.render(many(count))
    assert kinds([payload]) == ["carousel"]
    deck = payload["interactive"]["action"]["cards"]
    assert [c["card_index"] for c in deck] == list(range(count))
    assert [c["header"]["image"]["link"] for c in deck] == [
        f"{CDN}/DH6-{101 + n}.jpg" for n in range(count)
    ]
    for rank, card in enumerate(deck, 1):
        code = f"DH6-{100 + rank}"
        assert card["body"]["text"].startswith(f"#{rank} {code} ·")
        assert [b["quick_reply"]["id"] for b in card["action"]["buttons"]] == [
            f"photos:{code}", f"visit:{code}",
        ]
    lines = body(payload).splitlines()
    assert lines[0] == f"{count} matching listings:"
    assert [line.split(" ·")[0] for line in lines[1:-1]] == [
        f"#{n} DH6-{100 + n}" for n in range(1, count + 1)
    ]
    assert lines[-1] == "Reply with a number, or tap a card."


@pytest.mark.parametrize(("count", "rows"), [(7, 2), (11, 6), (15, 10)])
def test_more_than_five_adds_an_overflow_list(count, rows):
    payloads = cards.render(many(count))
    assert kinds(payloads) == ["carousel", "list"]
    assert len(payloads[0]["interactive"]["action"]["cards"]) == 5
    assert f"#6-#{count} are in the list below." in body(payloads[0])
    listed = payloads[1]["interactive"]["action"]["sections"][0]["rows"]
    assert len(listed) == rows
    assert listed[0]["id"] == "listing:DH6-106" and listed[0]["title"] == "#6 DH6-106"


def test_more_results_than_can_be_presented_is_an_error():
    with pytest.raises(ValueError):
        cards.render(many(cards.RESULTS_MAX + 1))


def test_a_listing_without_a_cover_keeps_its_number_and_follows_as_a_text_card():
    results = [listing("DH6-101"), listing("DH6-102", cover=False), listing("DH6-103")]
    payloads = cards.render(results)
    assert kinds(payloads) == ["carousel", "button"]
    deck = payloads[0]["interactive"]["action"]["cards"]
    assert [c["body"]["text"].split(" ·")[0] for c in deck] == ["#1 DH6-101", "#3 DH6-103"]
    assert "#2 DH6-102 · House for sale · DHA Phase 6 · PKR 7.8 crore (photos coming soon)" \
        in body(payloads[0])
    assert payloads[1]["interactive"]["header"]["text"] == "DH6-102 · photos coming soon"
    assert body(payloads[1]).startswith("#2 DH6-102")
    assert button_ids(payloads[1]) == ["photos:DH6-102", "visit:DH6-102"]


@pytest.mark.parametrize("results", [
    [listing("DH6-101"), listing("DH6-102", cover=False)],
    many(3, covered=False),
])
def test_fewer_than_two_covers_means_individual_cards(results):
    payloads = cards.render(results)
    assert kinds(payloads) == ["text"] + ["button"] * len(results)
    assert body(payloads[0]).endswith("Reply with a number, or tap a card's button.")
    for rank, (payload, item) in enumerate(zip(payloads[1:], results, strict=True), 1):
        header = payload["interactive"]["header"]
        assert header["type"] == ("image" if item.cover_url else "text")
        assert body(payload).startswith(f"#{rank} {item.code}")


def test_fallback_sends_individual_image_cards_with_the_carousel_buttons():
    payloads = cards.render(many(7), fallback=True)
    assert kinds(payloads) == ["text"] + ["button"] * 5 + ["list"]
    for n, payload in enumerate(payloads[1:6]):
        code = f"DH6-{101 + n}"
        assert payload["interactive"]["header"]["image"]["link"] == f"{CDN}/{code}.jpg"
        assert button_ids(payload) == [f"photos:{code}", f"visit:{code}"]
        assert len(body(payload)) <= whatsapp.CARD_BODY_MAX


def test_carousel_cards_repeat_exactly_what_the_carousel_showed():
    carousel = cards.render(many(3))[0]
    rebuilt = cards.carousel_cards(carousel)
    assert rebuilt == cards.carousel_cards(carousel)  # deterministic
    assert kinds(rebuilt) == ["text", "button", "button", "button"]
    assert body(rebuilt[0]) == body(carousel)
    for card, payload in zip(carousel["interactive"]["action"]["cards"], rebuilt[1:],
                             strict=True):
        assert payload["interactive"]["header"] == card["header"]
        assert body(payload) == card["body"]["text"]
        assert button_ids(payload) == [b["quick_reply"]["id"] for b in card["action"]["buttons"]]


def test_fallback_does_not_change_the_single_result_card():
    assert cards.render([listing()], fallback=True) == cards.render([listing()])


def dataset_listings():
    names = {row["key"]: row["display_name"] or row["name"]
             for row in csv.DictReader((dd.DEMO_DIR / "locations.csv").open(encoding="utf-8"))}
    out = []
    for row in csv.DictReader((dd.DEMO_DIR / "properties.csv").open(encoding="utf-8")):
        def dec(name, row=row):
            return Decimal(row[name]) if row[name] else None

        def num(name, row=row):
            return int(row[name]) if row[name] else None

        out.append(cards.Listing(
            row["listing_code"], row["purpose"] or None, row["property_type"] or None,
            num("price_pkr"), dec("area_value"), row["area_unit"] or None,
            dec("size_nominal_sqyd"), num("bedrooms"), num("bathrooms"),
            row["construction_status"] or None, names[row["location_key"]],
            f"{CDN}/{uuid.uuid4()}.jpg",
        ))
    return out


def test_every_dataset_listing_fits_every_format():
    every = dataset_listings()
    assert len(every) == 30
    for item in every:
        cards.render([item])
        body_text = cards.card_body(15, item)
        assert len(body_text) <= whatsapp.CARD_BODY_MAX and body_text.count("\n") <= 2
    for start in range(0, len(every), cards.SHOWN_MAX):
        chunk = every[start:start + cards.SHOWN_MAX] or every[:2]
        if len(chunk) >= 2:
            cards.render(chunk)
            cards.render(chunk, fallback=True)
    cards.render(every[:cards.RESULTS_MAX])
    cards.render(every[-cards.RESULTS_MAX:], fallback=True)


def test_long_values_are_trimmed_to_the_card_limit():
    item = listing(location="L" * 200)
    text = cards.card_body(1, item)
    assert len(text) == whatsapp.CARD_BODY_MAX and text.count("\n") == 2
    assert text.splitlines()[0] == "#1 DH6-101 · House for sale"


# --- database: migration 011 and listing reads ---------------------------------------

PNID, OTHER_PNID = "100000000000051", "100000000000052"
ADMIN, AGENT, BROKER = (seed.normalize_phone(SEED_ENV[name]) for name in
                        ("DEMO_ADMIN_PHONE", "DEMO_STAFF_PHONE", "DEMO_BROKER_PHONE"))
CUSTOMER = "923000000051"


@pytest.fixture
def demo(app_role_db):
    admin_url, app_url, _ = app_role_db
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, seed.DEMO_AGENCY_NAME, "demo-" + uuid.uuid4().hex, PNID)
    result = seed.run(agency, admin_url, app_url, SEED_ENV, apply=True)
    assert not result.conflicts
    other = create_agency(conn, "Other Realty (DEMO)", "other-" + uuid.uuid4().hex, OTHER_PNID)
    for code in echo.DEMO_LISTINGS:
        add_cover(conn, agency, code)

    client = FakeClient()
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0",
        s3_bucket=None, site_address="demo.example",
    )
    worker = Worker(app_url, [agency, other], make_handlers(settings, client_factory=lambda _: client),
                    threading.Event(), backoff=lambda n: 0)
    agencies = {PNID: agency, OTHER_PNID: other}

    def store(value, pnid, drain):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": pnid}, **value,
        }}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(conn, agencies[pnid]):
            for event in events:
                store_event(conn, agencies[pnid], event)
        while drain and worker.run_once():
            pass

    def deliver(*messages, pnid=PNID, drain=True):
        store({"messages": list(messages)}, pnid, drain)

    def status(wamid, state, *, recipient=ADMIN, code=None):
        """A Meta status webhook for one of our sent messages."""
        update = {"id": wamid, "status": state, "timestamp": str(int(time.time())),
                  "recipient_id": recipient}
        if code is not None:
            update["errors"] = [{"code": code, "title": "fictional error"}]
        store({"statuses": [update]}, PNID, True)

    def drain():
        while worker.run_once():
            pass

    def query(sql, params=(), *, agency_id=agency):
        with tenant_transaction(conn, agency_id):
            cursor = conn.execute(sql, params)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    yield SimpleNamespace(conn=conn, agency=agency, other=other, client=client, worker=worker,
                          deliver=deliver, status=status, drain=drain, query=query)
    worker.close()
    conn.close()


def add_cover(conn, agency, code):
    with tenant_transaction(conn, agency):
        prop = conn.execute("SELECT id FROM property WHERE listing_code = %s",
                            (code,)).fetchone()[0]
        token = uuid.uuid4()
        media_id = conn.execute(
            "INSERT INTO property_media (agency_id, property_id, original_key, card_image_key, "
            "public_url_card, sort_order, content_sha256, label) VALUES (current_agency_id(), "
            "%s, %s, %s, %s, 0, %s, 'exterior') RETURNING id",
            (prop, f"tenant/{agency}/{token}/original.jpg", f"card/{agency}/{token}.jpg",
             f"{CDN}/{token}.jpg", uuid.uuid4().hex * 2),
        ).fetchone()[0]
        conn.execute("UPDATE property SET cover_media_id = %s WHERE id = %s", (media_id, prop))


def text(wamid, body, *, sender=ADMIN):
    return {"from": sender, "id": wamid, "timestamp": str(int(time.time())),
            "type": "text", "text": {"body": body}}


def tap(wamid, reply_id, *, kind="button_reply", sender=CUSTOMER, context="wamid.OUT1"):
    """A tap webhook. kind "button" is a carousel quick reply, in the shape a real
    device delivered on 2026-10-10 (Meta's carousel docs do not show it)."""
    message = {"from": sender, "id": wamid, "timestamp": str(int(time.time())),
               "context": {"from": "15550009999", "id": context}}
    if kind == "button":
        return {**message, "type": "button",
                "button": {"text": "More photos", "payload": reply_id}}
    return {**message, "type": "interactive",
            "interactive": {"type": kind, kind: {"id": reply_id, "title": "More photos"}}}


def covers(d):
    return dict(d.query(
        "SELECT p.listing_code, m.public_url_card FROM property p "
        "JOIN property_media m ON m.id = p.cover_media_id"
    ))


def conversation_flag(d, sender):
    return d.query(
        "SELECT c.carousel_fallback, c.carousel_fallback_set_at IS NOT NULL, s.role "
        "FROM conversation c LEFT JOIN staff s ON s.id = c.carousel_fallback_set_by "
        "WHERE c.customer_wa_id = %s", (sender,),
    )[0]


def test_load_listings_reads_public_rows_in_order_with_covers(demo):
    rows = cards.load_listings(demo.conn, demo.agency, ["PRJ-103", "CLF-104", "DH6-102",
                                                        "DH6-101", "ZZZ-999"])
    # CLF-104 is rented (hidden); ZZZ-999 does not exist.
    assert [r.code for r in rows] == ["PRJ-103", "DH6-102", "DH6-101"]
    assert rows[0].cover_url == covers(demo)["PRJ-103"] and rows[1].cover_url is None
    assert rows[2].location == "DHA Phase 6" and rows[2].price_pkr == 78_000_000
    assert cards.load_listings(demo.conn, demo.other, ["DH6-101"]) == []


def test_a_cover_without_a_card_derivative_counts_as_no_cover(demo):
    demo.query("UPDATE property_media SET card_image_key = NULL")
    assert [r.cover_url for r in cards.load_listings(demo.conn, demo.agency,
                                                     echo.DEMO_LISTINGS)] == [None, None]


def test_new_conversations_start_without_the_fallback(demo):
    demo.deliver(text("wamid.C1", "hi", sender=CUSTOMER))
    assert conversation_flag(demo, CUSTOMER) == (False, False, None)


@pytest.mark.parametrize("statement", [
    "UPDATE conversation SET carousel_fallback = true",
    ("UPDATE conversation SET carousel_fallback_set_by = "
     "(SELECT id FROM staff WHERE role = 'admin')"),
])
def test_a_set_flag_or_setter_needs_a_change_time(demo, statement):
    demo.deliver(text("wamid.C1", "hi", sender=CUSTOMER))
    with pytest.raises(psycopg.errors.CheckViolation):
        demo.query(statement)


def test_the_flag_setter_must_be_staff_of_the_same_agency(demo):
    demo.deliver(text("wamid.C1", "hi", sender=CUSTOMER))
    with tenant_transaction(demo.conn, demo.other):
        foreign = demo.conn.execute(
            "INSERT INTO staff (agency_id, name, whatsapp_number, role) "
            "VALUES (current_agency_id(), 'Other (DEMO)', '923000000099', 'admin') RETURNING id"
        ).fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        demo.query("UPDATE conversation SET carousel_fallback = true, "
                   "carousel_fallback_set_at = now(), carousel_fallback_set_by = %s", (foreign,))


def test_the_flag_is_invisible_to_other_tenants(demo):
    demo.deliver(text("wamid.S1", "#demo fallback on"))
    assert demo.query("SELECT count(*) FROM conversation", agency_id=demo.other) == [(0,)]
    assert demo.query("UPDATE conversation SET carousel_fallback = false RETURNING id",
                      agency_id=demo.other) == []
    assert conversation_flag(demo, ADMIN)[0] is True


# --- database: staff '#demo' card commands -------------------------------------------

def sent_kinds(d, start=0):
    return [(to, kinds([p])[0]) for to, p in d.client.sent[start:]]


def test_demo_carousel_sends_the_pilot_listings_to_the_sender(demo):
    demo.deliver(text("wamid.S1", "#demo carousel"))
    [(to, payload)] = demo.client.sent
    assert to == ADMIN and kinds([payload]) == ["carousel"]
    deck = payload["interactive"]["action"]["cards"]
    urls = covers(demo)
    assert [c["header"]["image"]["link"] for c in deck] == [urls["DH6-101"], urls["PRJ-103"]]
    assert [c["action"]["buttons"][0]["quick_reply"]["id"] for c in deck] == [
        "photos:DH6-101", "photos:PRJ-103",
    ]
    assert demo.query("SELECT status, dedupe_key LIKE 'turn:%%:0' FROM outbound_message") == [
        ("sent", True),
    ]


def test_demo_cards_always_sends_individual_cards(demo):
    demo.deliver(text("wamid.S1", "#demo cards", sender=AGENT))
    assert sent_kinds(demo) == [(AGENT, "text"), (AGENT, "button"), (AGENT, "button")]
    assert conversation_flag(demo, AGENT)[0] is False


def test_demo_card_sends_the_single_result_card(demo):
    demo.deliver(text("wamid.S1", "#demo card"))
    [(to, payload)] = demo.client.sent
    assert to == ADMIN and button_ids(payload) == [
        "photos:DH6-101", "visit:DH6-101", "similar:DH6-101",
    ]
    assert payload["interactive"]["header"]["image"]["link"] == covers(demo)["DH6-101"]


@pytest.mark.parametrize(("sender", "stub"), [
    (CUSTOMER, "Customer mode (stub) · 1 message in this turn"),
    (BROKER, BROKER_HELP),   # Task 11.2: a broker gets the lead-command help (R-15)
])
def test_card_commands_are_for_staff_only(demo, sender, stub):
    for n, command in enumerate(["#demo carousel", "#demo cards", "#demo fallback on"]):
        demo.deliver(text(f"wamid.X{n}", command, sender=sender))
    assert demo.client.sent == [(sender, whatsapp.text(stub))] * 3
    assert conversation_flag(demo, sender)[0] is False


def test_a_phone_number_in_the_command_is_never_a_recipient(demo):
    demo.deliver(text("wamid.S1", "#demo carousel 923000000099"),
                 text("wamid.S2", "#demo fallback on 923000000099"))
    demo.deliver(text("wamid.S3", "#demo cards +92 300 0000099"))
    assert {to for to, _ in demo.client.sent} == {ADMIN}
    assert all(body(p).startswith("Try: #demo ") for _, p in demo.client.sent)
    assert demo.query("SELECT DISTINCT recipient_wa_id FROM outbound_message") == [(ADMIN,)]
    assert conversation_flag(demo, ADMIN)[0] is False


def test_fallback_flag_round_trip_changes_only_the_senders_conversation(demo):
    demo.deliver(text("wamid.C1", "hi", sender=CUSTOMER))
    demo.deliver(text("wamid.S1", "#demo fallback on"))
    assert body(demo.client.sent[-1][1]).startswith(
        "Carousel fallback is ON for this number: results come as individual cards. (changed "
    )
    assert conversation_flag(demo, ADMIN) == (True, True, "admin")
    assert conversation_flag(demo, CUSTOMER) == (False, False, None)

    start = len(demo.client.sent)
    demo.deliver(text("wamid.S2", "#demo carousel"))
    assert sent_kinds(demo, start) == [(ADMIN, "text"), (ADMIN, "button"), (ADMIN, "button")]

    demo.deliver(text("wamid.S3", "#demo fallback status"))
    assert body(demo.client.sent[-1][1]).startswith("Carousel fallback is ON")

    demo.deliver(text("wamid.S4", "#demo fallback off", sender=ADMIN))
    assert body(demo.client.sent[-1][1]).startswith("Carousel fallback is OFF")
    assert conversation_flag(demo, ADMIN) == (False, True, "admin")
    demo.deliver(text("wamid.S5", "#demo carousel"))
    assert sent_kinds(demo)[-1] == (ADMIN, "carousel")


def test_agents_may_set_their_own_flag(demo):
    demo.deliver(text("wamid.S1", "#demo fallback on", sender=AGENT))
    assert conversation_flag(demo, AGENT) == (True, True, "agent")


def test_a_gated_conversation_neither_sends_nor_changes_the_flag(demo):
    demo.deliver(text("wamid.S1", "#demo fallback status"))
    demo.query("UPDATE conversation SET mode = 'human', control_state = 'human_owned'")
    start = len(demo.client.sent)
    demo.deliver(text("wamid.S2", "#demo fallback on"))
    demo.deliver(text("wamid.S3", "#demo carousel"))
    assert demo.client.sent[start:] == []
    assert conversation_flag(demo, ADMIN)[0] is False


def test_a_listing_without_a_cover_is_still_delivered(demo):
    demo.query("UPDATE property SET cover_media_id = NULL WHERE listing_code = 'PRJ-103'")
    demo.deliver(text("wamid.S1", "#demo carousel"))
    assert sent_kinds(demo) == [(ADMIN, "text"), (ADMIN, "button"), (ADMIN, "button")]
    prj = demo.client.sent[2][1]
    assert prj["interactive"]["header"] == {"type": "text", "text": "PRJ-103 · photos coming soon"}
    assert "#2 PRJ-103" in body(demo.client.sent[0][1])


def test_a_hidden_demo_listing_is_reported_not_shown(demo, monkeypatch):
    monkeypatch.setattr(echo, "DEMO_LISTINGS", ("DH6-101", "CLF-104", "PRJ-103"))
    demo.deliver(text("wamid.S1", "#demo carousel"))
    assert sent_kinds(demo) == [(ADMIN, "text"), (ADMIN, "carousel")]
    assert body(demo.client.sent[0][1]) == \
        "DEMO: not a public listing of this agency: CLF-104."
    assert "CLF-104" not in str(demo.client.sent[1][1])


def test_a_refused_carousel_falls_back_to_individual_cards_in_the_same_turn(demo):
    demo.client.outcomes = [whatsapp.PermanentSendError("http_400_meta_131009")]
    demo.deliver(text("wamid.S1", "#demo carousel"))
    assert sent_kinds(demo) == [(ADMIN, "text"), (ADMIN, "button"), (ADMIN, "button")]
    assert demo.query(
        "SELECT status, last_error_code, dedupe_key LIKE '%%:fallback:%%' FROM outbound_message "
        "ORDER BY created_at"
    ) == [("failed", "http_400_meta_131009", False)] + [("sent", None, True)] * 3
    assert demo.query("SELECT status FROM conversation_turn") == [("replied",)]
    assert demo.query("SELECT status FROM job WHERE job_type = 'process_message'") == [
        ("succeeded",)
    ]
    # The flag is set by staff after a device test, never automatically.
    assert conversation_flag(demo, ADMIN)[0] is False


def test_replayed_demo_commands_send_nothing_twice(demo):
    message = text("wamid.S1", "#demo carousel")
    demo.deliver(message)
    demo.deliver(message)
    assert len(demo.client.sent) == 1
    assert demo.query("SELECT count(*) FROM outbound_message") == [(1,)]


# --- database: stable turn plans and carousel fallbacks ------------------------------

def outbound_rows(d, like="%"):
    return d.query("SELECT dedupe_key, status, last_error_code FROM outbound_message "
                   "WHERE dedupe_key LIKE %s ORDER BY dedupe_key", (like,))


def carousel_row(d):
    [(outbound_id, payload)] = d.query(
        "SELECT id, payload FROM outbound_message WHERE payload->'interactive'->>'type' "
        "= 'carousel'"
    )
    return outbound_id, payload


def fallback_jobs(d):
    return d.query("SELECT dedupe_key, status FROM job WHERE job_type = %s",
                   (cards.FALLBACK_JOB_TYPE,))


def test_the_turn_plan_is_stored_once_and_must_be_a_list(demo):
    demo.deliver(text("wamid.S1", "#demo carousel"))
    [(plan,)] = demo.query("SELECT reply_plan FROM conversation_turn")
    assert plan == [demo.client.sent[0][1]]
    with pytest.raises(psycopg.errors.CheckViolation):
        demo.query("UPDATE conversation_turn SET reply_plan = '{}'::jsonb")


def test_a_retry_after_an_immediate_refusal_sends_the_same_cards_once(demo):
    demo.client.outcomes = [whatsapp.PermanentSendError("http_400_meta_131009"),
                            whatsapp.RetryableSendError("http_503")]
    demo.deliver(text("wamid.S1", "#demo carousel"), drain=False)
    assert demo.worker.run_once()  # refused carousel, then the first card hits a 503
    assert demo.client.sent == []
    assert demo.query("SELECT status FROM conversation_turn") == [("open",)]

    demo.drain()  # the retry resumes the same turn and plan
    _, payload = carousel_row(demo)
    assert [p for _, p in demo.client.sent] == cards.carousel_cards(payload)
    rows = outbound_rows(demo)
    assert [(key.split(":", 2)[2], state, code) for key, state, code in rows] == [
        ("0", "failed", "http_400_meta_131009"),
        ("0:fallback:0", "sent", None), ("0:fallback:1", "sent", None),
        ("0:fallback:2", "sent", None),
    ]
    assert demo.query("SELECT status FROM conversation_turn") == [("replied",)]


def test_a_retried_turn_keeps_its_plan_when_the_flag_and_covers_change(demo, monkeypatch):
    monkeypatch.setattr(echo, "DEMO_LISTINGS", ("DH6-101", "PRJ-103", "DH6-102"))
    demo.client.outcomes = [None, whatsapp.RetryableSendError("http_503")]
    demo.deliver(text("wamid.S1", "#demo carousel"), drain=False)
    assert demo.worker.run_once()  # the carousel is sent, DH6-102's text card hits a 503
    assert sent_kinds(demo) == [(ADMIN, "carousel")]

    # Between the attempts the sender turns the fallback on and DH6-102 gets a cover:
    # a fresh render would now be individual image cards for all three listings.
    [(conversation,)] = demo.query("SELECT id FROM conversation")
    cards.set_fallback(demo.conn, demo.agency, conversation, True, None)
    add_cover(demo.conn, demo.agency, "DH6-102")

    demo.drain()
    assert sent_kinds(demo) == [(ADMIN, "carousel"), (ADMIN, "button")]
    assert demo.client.sent[1][1]["interactive"]["header"] == {
        "type": "text", "text": "DH6-102 · photos coming soon",
    }
    assert [state for _, state, _ in outbound_rows(demo)] == ["sent", "sent"]

    # A new turn does use the changed state.
    start = len(demo.client.sent)
    demo.deliver(text("wamid.S2", "#demo carousel"))
    assert sent_kinds(demo, start) == [(ADMIN, "text")] + [(ADMIN, "button")] * 3


def test_a_carousel_failed_after_acceptance_falls_back_once(demo):
    demo.deliver(text("wamid.S1", "#demo carousel"))
    outbound_id, payload = carousel_row(demo)
    demo.status("wamid.OUT1", "sent")
    demo.status("wamid.OUT1", "failed", code=131053)

    assert [p for _, p in demo.client.sent[1:]] == cards.carousel_cards(payload)
    assert {to for to, _ in demo.client.sent} == {ADMIN}
    assert outbound_rows(demo, "carousel-fallback:%") == [
        (f"carousel-fallback:{outbound_id}:{n}", "sent", None) for n in range(3)
    ]
    assert fallback_jobs(demo) == [(f"carousel-fallback:{outbound_id}", "succeeded")]

    # A replayed webhook, and a second hook call for the same row, add nothing.
    demo.status("wamid.OUT1", "failed", code=131053)
    with tenant_transaction(demo.conn, demo.agency):
        cards.on_failed_status(demo.conn, outbound_id, ADMIN, payload, delivered=False)
    demo.drain()
    assert len(demo.client.sent) == 4 and len(fallback_jobs(demo)) == 1


def test_a_delivered_carousel_that_later_fails_is_not_resent(demo):
    demo.deliver(text("wamid.S1", "#demo carousel"))
    demo.status("wamid.OUT1", "delivered")
    demo.status("wamid.OUT1", "failed", code=131053)
    assert len(demo.client.sent) == 1 and fallback_jobs(demo) == []


def test_a_failed_fallback_card_is_never_fallen_back_again(demo):
    demo.deliver(text("wamid.S1", "#demo carousel"))
    demo.status("wamid.OUT1", "failed", code=131053)
    assert len(demo.client.sent) == 4
    for wamid in ("wamid.OUT2", "wamid.OUT3", "wamid.OUT4"):
        demo.status(wamid, "failed", code=131053)
    assert len(demo.client.sent) == 4 and len(fallback_jobs(demo)) == 1


def test_an_ambiguous_carousel_is_never_resent_or_fallen_back(demo):
    demo.client.outcomes = [whatsapp.AmbiguousSendError("ReadTimeout")]
    demo.deliver(text("wamid.S1", "#demo carousel"))
    assert demo.client.sent == []
    [(_, state, code)] = outbound_rows(demo)
    assert (state, code) == ("unknown", "ReadTimeout")
    assert demo.query("SELECT provider_message_id FROM outbound_message") == [(None,)]
    demo.deliver(text("wamid.S1", "#demo carousel"))  # a replay
    assert demo.client.sent == [] and fallback_jobs(demo) == []
    assert demo.query("SELECT status FROM conversation_turn") == [("replied",)]


def test_the_fallback_job_goes_through_the_send_time_gate(demo):
    demo.deliver(text("wamid.S1", "#demo carousel"))
    demo.query("UPDATE conversation SET mode = 'human', control_state = 'human_owned'")
    demo.status("wamid.OUT1", "failed", code=131053)
    assert len(demo.client.sent) == 1
    [(key, state, code)] = outbound_rows(demo, "carousel-fallback:%")
    assert key.endswith(":0") and (state, code) == ("cancelled", "control_gate")
    assert [state for _, state in fallback_jobs(demo)] == ["succeeded"]


# --- database: taps and quoted replies -----------------------------------------------

@pytest.mark.parametrize(("reply_id", "kind", "expected"), [
    ("photos:DH6-101", "button_reply", "More photos for DH6-101 (stub)"),
    ("visit:PRJ-103", "button_reply", "Visit / Details for PRJ-103 (stub)"),
    ("similar:DH6-101", "button_reply", "Similar listings for DH6-101 (stub)"),
    ("listing:DH6-104", "list_reply", "Details for DH6-104 (stub)"),
    ("photos:DH6-101", "button", "More photos for DH6-101 (stub)"),
    ("visit:PRJ-103", "button", "Visit / Details for PRJ-103 (stub)"),
])
def test_a_card_tap_gets_a_stub_naming_the_action(demo, reply_id, kind, expected):
    demo.deliver(tap("wamid.T1", reply_id, kind=kind))
    [(to, payload)] = demo.client.sent
    assert to == CUSTOMER
    assert body(payload) == f"{expected} · coming in a later task"
    assert demo.query("SELECT message_type, text_body, context_wamid FROM message "
                      "WHERE provider_message_id = 'wamid.T1'") == [
        ("button" if kind == "button" else "interactive", "More photos", "wamid.OUT1"),
    ]


def test_a_tap_on_a_sent_carousel_card_names_that_card(demo):
    demo.deliver(text("wamid.S1", "#demo carousel"))
    [(_, carousel)] = demo.client.sent
    deck = carousel["interactive"]["action"]["cards"]
    reply_id = deck[1]["action"]["buttons"][0]["quick_reply"]["id"]
    assert reply_id == "photos:PRJ-103"
    demo.deliver(tap("wamid.T1", reply_id, kind="button", sender=ADMIN))
    assert body(demo.client.sent[-1][1]) == (
        "More photos for PRJ-103 (stub) · coming in a later task"
    )


@pytest.mark.parametrize("reply_id", [
    "photos:<b>DH6-101</b>", "photos:dh6-101", "photos:DH6-101\n", "delete:DH6-101",
    "demo_btn_1", "",
])
@pytest.mark.parametrize("kind", ["button_reply", "button"])
def test_other_tap_ids_are_never_echoed(demo, reply_id, kind):
    demo.deliver(tap("wamid.T1", reply_id, kind=kind, sender=AGENT))
    assert demo.client.sent == [(AGENT, whatsapp.text(
        "Staff mode (stub) · 1 message in this turn"
    ))]


def test_quoted_text_keeps_its_context_and_plain_text_has_none(demo):
    quoted = dict(text("wamid.C1", "is #2 still available?", sender=CUSTOMER),
                  context={"from": "15550009999", "id": "wamid.OUT7"})
    demo.deliver(quoted, text("wamid.C2", "thanks", sender=CUSTOMER))
    assert demo.query("SELECT provider_message_id, context_wamid FROM message "
                      "WHERE direction = 'inbound' ORDER BY provider_message_id") == [
        ("wamid.C1", "wamid.OUT7"), ("wamid.C2", None),
    ]


def test_a_blank_context_id_is_ignored(demo):
    demo.deliver(dict(text("wamid.C1", "hi", sender=CUSTOMER), context={"id": "  "}))
    assert demo.query("SELECT context_wamid FROM message WHERE direction = 'inbound'") == [
        (None,)
    ]
