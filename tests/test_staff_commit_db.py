"""Task 9.2 acceptance: staff confirmation and commit through the real worker, orchestrator,
staff buffer, turn engine, outbound path and gazetteer cache, on the seeded demo agency
(30 fictional listings), as the restricted manzil_app login (RLS on).

Gemini is a fake transport (the model's spans are scripted); WhatsApp is the FakeClient of
tests/test_gateway.py (each send gets wamid.OUT{n}); photo downloads are simulated by marking
the staged inbound_media rows stored. Covers Demo Scope scenario 4a-4c, the double tap, undo,
Edit, reply-to-card and the photo-only list. All data is fictional.
"""

import itertools
import json
import threading
import time
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from app import (
    gazetteer,
    orchestrator,
    requirements,
    seed,
    staff_actions,
    staff_tools,
    webhook,
    whatsapp,
)
from app.db import tenant_transaction
from app.handlers import make_handlers
from app.ingest import store_event
from app.llm import GeminiClient
from app.provision_agency import create_agency
from app.staff_cards import NOTICES
from app.turn_engine import TEMPLATES, TurnEngine
from app.worker import Worker
from tests.test_gateway import FakeClient
from tests.test_seed import ENV
from tests.test_staff_drafts import ST
from tests.test_turn_engine import (
    FAILOVER,
    KEY,
    PRIMARY,
    FakeClock,
    answer,
    flush_staff_segments,
    tool,
    tools_answer,
)

PNID = "100000000000001"
STAFF = "15550000102"  # the seeded agent (tests/test_seed.py ENV)
ADMIN = "15550000101"
_ids = itertools.count(1)


@pytest.fixture
def sc(app_role_db):
    admin_url, app_url, _ = app_role_db
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, seed.DEMO_AGENCY_NAME, "demo", PNID)
    assert seed.run(agency, admin_url, app_url, ENV, apply=True).conflicts == []
    cache = gazetteer.GazetteerCache.from_database_url(app_url)
    cache.load()
    service = requirements.RequirementService(cache)
    script, clock = [], FakeClock()

    def handler(request):
        return httpx.Response(200, json=script.pop(0))

    engine = TurnEngine(GeminiClient(KEY, transport=httpx.MockTransport(handler)),
                        PRIMARY, FAILOVER, clock=clock, requirements=service)
    client = FakeClient()
    settings = SimpleNamespace(meta_access_token="unused", meta_api_version="v25.0",
                               s3_bucket=None, site_address="demo.example")
    handlers = make_handlers(settings, client_factory=lambda pnid: client, engine=engine)
    worker = Worker(app_url, [agency], handlers, threading.Event(), backoff=lambda n: 0)

    def deliver(*messages):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": PNID}, "messages": list(messages)}}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(conn, agency):
            for event in events:
                store_event(conn, agency, event)

    def drain():
        while worker.run_once():
            pass

    def query(sql, params=()):
        with tenant_transaction(conn, agency):
            return conn.execute(sql, params).fetchall()

    ns = SimpleNamespace(conn=conn, admin_url=admin_url, app_url=app_url, agency=agency,
                         client=client, script=script, deliver=deliver, drain=drain,
                         query=query, cache=cache)

    def send(*messages):
        """Deliver, run every job (segments flushed), return [(wamid, payload)] sent."""
        before = len(client.sent)
        deliver(*messages)
        drain()
        flush_staff_segments(ns)
        return [(f"wamid.OUT{before + i + 1}", payload)
                for i, (_, payload) in enumerate(client.sent[before:])]

    ns.send = send
    yield ns
    worker.close()
    conn.close()


def now():
    return str(int(time.time()))


def text(body, sender=STAFF, context=None):
    message = {"from": sender, "id": f"wamid.S{next(_ids)}", "timestamp": now(),
               "type": "text", "text": {"body": body}}
    if context:
        message["context"] = {"id": context}
    return message


def photo(sender=STAFF, context=None):
    message = {"from": sender, "id": f"wamid.P{next(_ids)}", "timestamp": now(),
               "type": "image", "image": {"id": f"media-{next(_ids)}", "mime_type": "image/jpeg"}}
    if context:
        message["context"] = {"id": context}
    return message


def tap(reply_id, context=None, *, kind="button_reply", title="Tap", sender=STAFF):
    message = {"from": sender, "id": f"wamid.B{next(_ids)}", "timestamp": now(),
               "type": "interactive",
               "interactive": {"type": kind, kind: {"id": reply_id, "title": title}}}
    if context:
        message["context"] = {"id": context}
    return message


def pick(reply_id, context=None):
    return tap(reply_id, context, kind="list_reply")


def store_photos(sc):
    """Simulate app.media's download: every pending staged photo becomes a stored copy."""
    sc.query(
        """
        UPDATE inbound_media SET status = 'stored', stored_at = now(), updated_at = now(),
            content_sha256 = encode(sha256(id::text::bytea), 'hex'), mime_type = 'image/jpeg',
            byte_size = 1000, width = 1600, height = 1200, exif_stripped = TRUE,
            original_key = 'tenant/demo/inbound/' || id || '/original.jpg',
            wa_image_key = 'tenant/demo/inbound/' || id || '/wa.jpg',
            card_image_key = 'card/demo/' || card_token || '.jpg',
            public_url_card = 'https://demo.example/card/' || card_token || '.jpg'
        WHERE status = 'pending' RETURNING id
        """)


def body(payload):
    return whatsapp.summary(payload)


def button_ids(payload):
    return [b["reply"]["id"] for b in payload["interactive"]["action"]["buttons"]]


def button(payload, prefix):
    [found] = [i for i in button_ids(payload) if i.startswith(prefix)]
    return found


def rows(payload):
    return [r["id"] for s in payload["interactive"]["action"]["sections"] for r in s["rows"]]


def listing(sc, code):
    found = sc.query("SELECT status, price_pkr, version, cover_media_id, created_by_staff_id "
                     "FROM property WHERE listing_code = %s", (code,))
    return found[0] if found else None


def public(sc, code):
    return sc.query("SELECT listing_code FROM v_public_listing WHERE listing_code = %s",
                    (code,)) != []


def events(sc, code):
    return sc.query("SELECT e.event_type FROM property_event e JOIN property p "
                    "ON p.id = e.property_id WHERE p.listing_code = %s "
                    "ORDER BY e.created_at, e.to_version", (code,))


def draft_once(sc, spans, *messages):
    """One segment answered by a scripted draft_listing call; returns the sent [(wamid, p)]."""
    sc.script += [tools_answer(tool("draft_listing", {"drafts": spans})), answer()]
    sc.deliver(*messages)
    sc.drain()
    store_photos(sc)
    return sc.send()


def scenario_4a(sc):
    """ST-01 + 3 photos -> card -> Save. Returns (card wamid, card, saved reply)."""
    [(wamid, card)] = draft_once(sc, [ST["ST-01"][1]], text(ST["ST-01"][0]), photo(), photo(),
                                 photo())
    [(_, saved)] = sc.send(tap(button(card, "draft:save:"), wamid, title="Save"))
    return wamid, card, saved


# --- scenario 4a ---------------------------------------------------------------------------

def test_the_live_turn_shape_bad_args_then_draft_then_an_empty_stop_still_sends_the_card(sc):
    # Seen live 2026-10-14 on gemini-3.5-flash-lite: the answer after the tool results was a
    # STOP without parts. It ends the turn; the backend card is the reply.
    empty = {"candidates": [{"finishReason": "STOP", "content": {"role": "model"}}]}
    sc.script += [tools_answer(tool("draft_listing", {"drafts": [{"rooms": 4}]}, "c1")),
                  tools_answer(tool("draft_listing", {"drafts": [ST["ST-01"][1]]}, "c2")),
                  empty]
    sc.deliver(text(ST["ST-01"][0]))
    sc.drain()
    [(_, card)] = sc.send()
    assert body(card).startswith("📝 *Draft 1 of 1* · Proposed ID *DH6-109*")
    assert sc.query("SELECT status FROM conversation_turn WHERE staff_segment_id IS NOT NULL") \
        == [("replied",)]


def test_4a_the_card_has_the_cover_and_save_creates_dh6_109_visible_at_once(sc):
    _, card, saved = scenario_4a(sc)
    assert card["type"] == "interactive" and card["interactive"]["header"]["type"] == "image"
    assert body(card).splitlines()[:7] == [
        "📝 *Draft 1 of 1* · Proposed ID *DH6-109*", "", "🏠 *HOUSE · SALE* (inferred)",
        "📍 DHA Phase 6 · 500 sq yd · 4 bed / 5 bath", "💰 *PKR 8 crore* · Owner direct", "",
        "📷 Photos: 3"]
    assert [b.split(":")[1] for b in button_ids(card)] == ["save", "edit", "cancel"]

    assert body(saved) == "✅ Saved as *DH6-109*. Listing ab active hai."
    assert button_ids(saved)[0].startswith("undo:")
    status, price, version, cover, _ = listing(sc, "DH6-109")
    assert (status, price, version) == ("active", 80_000_000, 1) and cover is not None
    assert public(sc, "DH6-109")  # customers can find it immediately (v_public_listing)
    assert sc.query("SELECT count(*), count(DISTINCT content_sha256), min(sort_order), "
                    "max(sort_order) FROM property_media m JOIN property p "
                    "ON p.id = m.property_id WHERE p.listing_code = 'DH6-109'") == [(3, 3, 0, 2)]
    assert events(sc, "DH6-109") == [("created",)]
    row = sc.query("SELECT p.size_nominal_sqyd, p.area_unit, p.bedrooms, p.bathrooms, "
                   "p.source_type, p.category, p.is_demo, p.source_text IS NOT NULL, "
                   "cardinality(p.source_message_ids) FROM property p "
                   "WHERE p.listing_code = 'DH6-109'")
    assert row == [(500, "sq_yd", 4, 5, "owner_direct", "residential", True, True, 4)]  # text + 3 photos
    draft = sc.query("SELECT status, closed_at IS NOT NULL FROM listing_draft "
                     "WHERE kind = 'create'")
    assert draft == [("saved", True)]


def test_a_double_tap_on_save_writes_nothing_more(sc):
    wamid, card, _ = scenario_4a(sc)
    before = sc.query("SELECT count(*) FROM property"), sc.query(
        "SELECT count(*) FROM property_event")
    [(_, again)] = sc.send(tap(button(card, "draft:save:"), wamid, title="Save"))
    assert body(again) == NOTICES["staff_already_saved"].format(code="DH6-109")
    assert (sc.query("SELECT count(*) FROM property"),
            sc.query("SELECT count(*) FROM property_event")) == before
    [(_, cancel)] = sc.send(tap(button(card, "draft:cancel:"), wamid))
    assert body(cancel) == NOTICES["staff_already_saved"].format(code="DH6-109")
    assert listing(sc, "DH6-109")[0] == "active"


def test_the_saved_reply_undo_withdraws_the_new_listing_once(sc):
    _, _, saved = scenario_4a(sc)
    [(_, undone)] = sc.send(tap(button(saved, "undo:"), title="Undo"))
    assert body(undone) == "↩ Undo ho gaya: *DH6-109* ab WITHDRAWN hai."
    assert listing(sc, "DH6-109")[:3] == ("withdrawn", 80_000_000, 2)
    assert not public(sc, "DH6-109")
    assert events(sc, "DH6-109") == [("created",), ("undone",)]
    [(_, twice)] = sc.send(tap(button(saved, "undo:"), title="Undo"))
    assert body(twice) == NOTICES["staff_undo_done_before"]
    assert listing(sc, "DH6-109")[2] == 2


def test_the_undo_button_lasts_30_minutes_then_undo_command_still_works(sc):
    _, _, saved = scenario_4a(sc)
    with psycopg.connect(sc.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE property_event SET created_at = now() - interval '31 minutes'")
    [(_, late)] = sc.send(tap(button(saved, "undo:"), title="Undo"))
    assert body(late) == NOTICES["staff_undo_expired"]
    assert listing(sc, "DH6-109")[0] == "active"
    [(_, ask)] = sc.send(text("#undo DH6-109"))  # a confirm card first: no write yet
    assert body(ask).startswith("↩ *Undo DH6-109*: listing hata di jayegi (WITHDRAWN)?")
    assert listing(sc, "DH6-109")[0] == "active"
    [(_, done)] = sc.send(tap(button(ask, "undoc:confirm:"), title="Confirm undo"))
    assert body(done) == "↩ Undo ho gaya: *DH6-109* ab WITHDRAWN hai."
    assert not public(sc, "DH6-109")


def test_cancel_closes_the_draft_and_writes_no_listing(sc):
    [(wamid, card)] = draft_once(sc, [ST["ST-01"][1]], text(ST["ST-01"][0]))
    [(_, done)] = sc.send(tap(button(card, "draft:cancel:"), wamid))
    assert body(done) == NOTICES["staff_draft_cancelled"]
    [(_, late)] = sc.send(tap(button(card, "draft:save:"), wamid))
    assert body(late) == NOTICES["staff_card_closed"]
    assert listing(sc, "DH6-109") is None


# --- scenario 4b -----------------------------------------------------------------------------

def propose(sc, message, code, *changes):
    sc.script += [tools_answer(tool("propose_update", {
        "target": {"code": code}, "changes": [{"field": f, "span": s} for f, s in changes]})),
        answer()]
    return sc.send(text(message))


def test_4b_update_by_id_confirms_with_the_facts_then_undo_restores_the_price(sc):
    [(wamid, card)] = propose(sc, "#DH6-102 price 7.8 crore", "DH6-102", ("price", "7.8 crore"))
    assert body(card).splitlines()[:4] == [
        "✏️ *DH6-102*: price PKR 8 crore → *PKR 7.8 crore*", "*Confirm?*", "",
        "🏠 *HOUSE · SALE · ACTIVE*"]
    assert listing(sc, "DH6-102")[1] == 80_000_000  # nothing written before the tap
    [(_, done)] = sc.send(tap(button(card, "draft:confirm:"), wamid, title="Confirm"))
    assert body(done) == "✅ *DH6-102* updated: price PKR 8 crore → *PKR 7.8 crore*."
    assert listing(sc, "DH6-102")[1:3] == (78_000_000, 2)
    assert sc.query("SELECT price_pkr FROM v_public_listing WHERE listing_code = 'DH6-102'") \
        == [(78_000_000,)]
    assert events(sc, "DH6-102")[-1] == ("updated",)

    [(_, again)] = sc.send(tap(button(card, "draft:confirm:"), wamid))
    assert body(again) == NOTICES["staff_already_updated"]  # double tap: no-op
    assert listing(sc, "DH6-102")[2] == 2

    [(_, undone)] = sc.send(tap(button(done, "undo:")))
    assert body(undone) == "↩ Undo ho gaya: *DH6-102* price PKR 7.8 crore → *PKR 8 crore*."
    assert listing(sc, "DH6-102")[1:3] == (80_000_000, 3)


def test_an_update_card_goes_stale_when_the_listing_changed_in_between(sc):
    [(wamid, card)] = propose(sc, "#DH6-102 price 7.8 crore", "DH6-102", ("price", "7.8 crore"))
    sc.query("UPDATE property SET version = version + 1 WHERE listing_code = 'DH6-102' "
             "RETURNING id")
    [(_, stale)] = sc.send(tap(button(card, "draft:confirm:"), wamid))
    assert body(stale) == NOTICES["staff_update_stale"]
    assert listing(sc, "DH6-102")[1] == 80_000_000
    assert sc.query("SELECT status FROM listing_draft WHERE kind = 'update'") == [
        ("superseded",)]


def test_an_undo_after_a_later_change_is_refused(sc):
    [(wamid, card)] = propose(sc, "#DH6-102 price 7.8 crore", "DH6-102", ("price", "7.8 crore"))
    [(_, done)] = sc.send(tap(button(card, "draft:confirm:"), wamid))
    [(wamid2, card2)] = propose(sc, "#DH6-102 reserved", "DH6-102", ("status", "reserved"))
    sc.send(tap(button(card2, "draft:confirm:"), wamid2))
    [(_, refused)] = sc.send(tap(button(done, "undo:")))
    assert body(refused) == NOTICES["staff_undo_later_change"]
    assert listing(sc, "DH6-102")[:2] == ("reserved", 78_000_000)


# --- scenario 4c ---------------------------------------------------------------------------

def test_4c_a_description_update_lists_the_houses_then_the_pick_confirms_sold(sc):
    scenario_4a(sc)
    sc.script += [tools_answer(tool("find_listings", {"location": "DHA 6",
                                                      "property_type": "ghar"})), answer()]
    [(_, choice)] = sc.send(text("DHA 6 wala ghar sold"))
    assert choice["interactive"]["type"] == "list"
    assert body(choice).startswith("Kaunsi property?")
    ids = rows(choice)
    assert "pick:DH6-109:s=sold" in ids and "pick:DH6-101:s=sold" in ids
    assert all(i.endswith(":s=sold") for i in ids)
    assert listing(sc, "DH6-109")[0] == "active"  # a list writes nothing

    [(wamid, card)] = sc.send(pick("pick:DH6-109:s=sold"))
    assert body(card).splitlines()[:2] == ["✏️ *DH6-109*: status active → *sold*",
                                           "*Confirm?*"]
    assert card["interactive"]["header"]["type"] == "image"  # R-11: the cover is shown
    [(_, done)] = sc.send(tap(button(card, "draft:confirm:"), wamid))
    assert body(done) == "✅ *DH6-109* marked *SOLD*."
    assert listing(sc, "DH6-109")[0] == "sold" and not public(sc, "DH6-109")
    assert events(sc, "DH6-109") == [("created",), ("status_changed",)]
    assert listing(sc, "DH6-101")[0] == "active"  # 0 wrong-property updates


def test_a_pick_is_revalidated_and_a_forged_change_is_refused(sc):
    [(_, refused)] = sc.send(pick("pick:DH6-107:s=sold"))  # DH6-107 is a rent listing
    assert body(refused) == "DH6-107: sold sirf sale listing ho sakti hai."
    [(_, unknown)] = sc.send(pick("pick:DH6-999:s=sold"))
    assert body(unknown) == "DH6-999 nahi mili."
    [(_, unclear)] = sc.send(pick("pick:DH6-101:"))
    assert body(unclear) == NOTICES["staff_pick_no_change"].format(code="DH6-101")
    assert sc.query("SELECT count(*) FROM listing_draft WHERE kind = 'update'") == [(0,)]


# --- Edit, stale versions, the save blockers -----------------------------------------------

def test_edit_fills_the_gaps_bumps_the_version_and_old_buttons_go_stale(sc):
    [(wamid, card)] = draft_once(sc, [ST["ST-07"][1]], text(ST["ST-07"][0]))
    assert [b.split(":")[1] for b in button_ids(card)] == ["edit", "cancel"]
    forged = button(card, "draft:edit:").replace(":edit:", ":save:")
    [(_, blocked)] = sc.send(tap(forged, wamid))  # a forged Save is refused (R-07)
    assert body(blocked) == NOTICES["staff_save_blocked"].format(fields="Price, Bedrooms")

    [(_, prompt)] = sc.send(tap(button(card, "draft:edit:"), wamid, title="Edit"))
    assert body(prompt) == NOTICES["staff_edit_prompt"]
    [(wamid2, card2)] = sc.send(text("price 7.5 crore, 4 bed"))
    assert "⚠" not in body(card2) and "PKR 7.5 crore" in body(card2)
    assert button(card2, "draft:save:").endswith(":2")
    [(_, stale)] = sc.send(tap(forged, wamid))
    assert body(stale) == NOTICES["staff_card_stale"]
    [(_, saved)] = sc.send(tap(button(card2, "draft:save:"), wamid2))
    assert body(saved) == "✅ Saved as *DH7-102*. Listing ab active hai."
    assert listing(sc, "DH7-102")[:2] == ("active", 75_000_000)


def test_a_text_reply_to_a_card_corrects_that_draft_without_an_edit_tap(sc):
    [(wamid, _)] = draft_once(sc, [ST["ST-07"][1]], text(ST["ST-07"][0]))
    [(_, card2)] = sc.send(text("8 crore 5 bed", context=wamid))
    assert "PKR 8 crore" in body(card2) and button(card2, "draft:save:").endswith(":2")
    assert sc.query("SELECT count(*) FROM staff_segment") == [(1,)]  # not a new segment


def test_a_new_listing_sent_during_an_edit_opens_a_segment_and_leaves_the_draft(sc):
    [(wamid, card)] = draft_once(sc, [ST["ST-07"][1]], text(ST["ST-07"][0]))
    sc.send(tap(button(card, "draft:edit:"), wamid))
    before = len(sc.client.sent)
    sc.deliver(text("Phase 8 ghar 500 gaz 7 crore sale 4 bed"))
    sc.drain()  # buffered, not answered as a correction
    assert len(sc.client.sent) == before
    assert sc.query("SELECT version FROM listing_draft") == [(1,)]
    assert sc.query("SELECT count(*) FROM staff_segment WHERE status = 'open'") == [(1,)]


def test_an_unclear_correction_keeps_the_edit_open(sc):
    [(wamid, card)] = draft_once(sc, [ST["ST-07"][1]], text(ST["ST-07"][0]))
    sc.send(tap(button(card, "draft:edit:"), wamid))
    [(_, unclear)] = sc.send(text("theek hai"))
    assert body(unclear) == NOTICES["staff_edit_unclear"]
    [(_, card2)] = sc.send(text("price 7 crore, 4 bed"))
    assert "PKR 7 crore" in body(card2)


# --- photos: reply-to-card and the photo-only list (R-08, R-10) -----------------------------

PASTE = ("Phase 6 ghar 500 gaz 4 bed 8 crore sale\n"
         "Clifton block 5 shop 350 sq ft 2.5 lakh rent")
TWO = [{"text": PASTE.split("\n")[0], "purpose": "sale", "property_type": "ghar",
        "location": "Phase 6", "size": "500 gaz", "price": "8 crore", "bedrooms": "4 bed"},
       {"text": PASTE.split("\n")[1], "purpose": "rent", "property_type": "shop",
        "location": "Clifton block 5", "size": "350 sq ft", "price": "2.5 lakh"}]


def test_photos_replying_to_one_of_several_cards_attach_to_that_draft_only(sc):
    (_, c1), (w2, c2) = draft_once(sc, TWO, text(PASTE), photo())
    assert "reply karke" in body(c1) and "reply karke" in body(c2)  # never auto-assigned
    [(_, again)] = sc.send(photo(context=w2), photo(context=w2))
    assert "📷 Photos: 2" in body(again) and body(again).startswith("📝 *Draft 2 of 2*")
    store_photos(sc)
    [(_, saved)] = sc.send(tap(button(again, "draft:save:"), title="Save"))
    assert body(saved) == "✅ Saved as *CLF-105*. Listing ab active hai."
    assert sc.query("SELECT count(*) FROM property_media m JOIN property p "
                    "ON p.id = m.property_id WHERE p.listing_code = 'CLF-105'") == [(2,)]
    [(_, first)] = sc.send(tap(button(c1, "draft:save:")))
    assert body(first) == "✅ Saved as *DH6-109*. Listing ab active hai."
    assert sc.query("SELECT count(*) FROM property_media m JOIN property p "
                    "ON p.id = m.property_id WHERE p.listing_code = 'DH6-109'") == [(0,)]


def test_a_photo_only_segment_lists_open_drafts_and_recent_listings(sc):
    draft_once(sc, [ST["ST-07"][1]], text(ST["ST-07"][0]))
    scenario_4a(sc)
    sc.deliver(photo(), photo())
    sc.drain()
    store_photos(sc)
    [(_, prompt)] = sc.send()
    assert body(prompt) == "📷 Ye 2 photos kis listing ki hain? List se chunein."
    targets = rows(prompt)
    draft_row = [r for r in targets if ":d:" in r]
    listing_row = [r for r in targets if ":p:" in r]
    assert len(draft_row) == 1 and len(listing_row) == 1  # the open ST-07 draft, DH6-109

    [(_, card2)] = sc.send(pick(draft_row[0]))
    assert "📷 Photos: 2" in body(card2) and body(card2).startswith("📝 *Draft 1 of 1*")

    [(_, ask)] = sc.send(pick(listing_row[0]))
    assert body(ask).startswith("📷 *DH6-109*: 2 photos add karein?")
    assert sc.query("SELECT count(*) FROM property_media m JOIN property p ON "
                    "p.id = m.property_id WHERE p.listing_code = 'DH6-109'") == [(3,)]
    [(_, added)] = sc.send(tap(button(ask, "attachc:confirm:")))
    assert body(added) == "✅ *DH6-109*: 2 photos add ho gayin."
    assert sc.query("SELECT count(*), max(sort_order) FROM property_media m JOIN property p "
                    "ON p.id = m.property_id WHERE p.listing_code = 'DH6-109'") == [(5, 4)]
    assert events(sc, "DH6-109")[-1] == ("media_added",)
    [(_, twice)] = sc.send(tap(button(ask, "attachc:confirm:")))
    assert body(twice) == NOTICES["staff_card_closed"]
    assert sc.query("SELECT count(*) FROM property_media m JOIN property p ON "
                    "p.id = m.property_id WHERE p.listing_code = 'DH6-109'") == [(5,)]


def test_without_drafts_or_recent_listings_photos_get_the_7_4_question(sc):
    sc.deliver(photo())
    sc.drain()
    store_photos(sc)
    [(_, reply)] = sc.send()
    assert body(reply) == TEMPLATES["staff_photo_only"]


# --- roles, tenants, the map ----------------------------------------------------------------

def test_another_staff_member_cannot_use_my_card(sc):
    [(wamid, card)] = draft_once(sc, [ST["ST-01"][1]], text(ST["ST-01"][0]))
    [(_, closed)] = sc.send(tap(button(card, "draft:save:"), wamid, sender=ADMIN))
    assert body(closed) == NOTICES["staff_card_closed"]
    assert listing(sc, "DH6-109") is None


def test_customer_taps_never_reach_the_staff_actions(sc):
    [(wamid, card)] = draft_once(sc, [ST["ST-01"][1]], text(ST["ST-01"][0]))
    sc.script += [answer()]
    sc.send(tap(button(card, "draft:save:"), wamid, sender="15550009999"))
    assert listing(sc, "DH6-109") is None


def test_every_staff_card_is_mapped_to_its_draft_or_listing(sc):
    wamid, _, _ = scenario_4a(sc)
    mapped = sc.query("SELECT c.dedupe_key, c.listing_draft_id IS NOT NULL, "
                      "c.property_id IS NOT NULL FROM staff_card c ORDER BY c.created_at")
    assert [(m[1], m[2]) for m in mapped] == [(True, False), (False, True)]
    assert sc.query("SELECT provider_message_id FROM outbound_message WHERE dedupe_key = %s",
                    (mapped[0][0],)) == [(wamid,)]
    json.dumps(mapped, default=str)


# --- retries, races, tenants, migration 017 ------------------------------------------------

def test_a_save_whose_reply_failed_to_send_is_retried_without_a_second_write(sc):
    [(wamid, card)] = draft_once(sc, [ST["ST-01"][1]], text(ST["ST-01"][0]))
    sc.client.outcomes.append(whatsapp.RetryableSendError("fictional_outage"))
    [(_, saved)] = sc.send(tap(button(card, "draft:save:"), wamid))  # the retry sends it
    assert body(saved) == "✅ Saved as *DH6-109*. Listing ab active hai."
    assert sc.query("SELECT count(*) FROM property WHERE listing_code LIKE 'DH6-1%%'") == [
        (9,)]  # DH6-101..108 + 109, nothing twice
    assert events(sc, "DH6-109") == [("created",)]


def staff_turn(sc):
    staff, conversation = sc.query(
        "SELECT s.id, c.id FROM staff s JOIN conversation c ON c.customer_wa_id = %s "
        "WHERE s.role = 'agent'", (STAFF,))[0]
    return orchestrator.Turn(
        id=sc.query("SELECT id FROM conversation_turn ORDER BY created_at DESC LIMIT 1")[0][0],
        agency_id=sc.agency, conversation_id=conversation, sender_wa_id=STAFF,
        sender_role="agent", route="staff", staff_id=staff, control_state="ai_active",
        conversation_mode="ai", messages=())


def tap_message(reply_id):
    import uuid

    return orchestrator.TurnMessage(uuid.uuid4(), f"wamid.X{next(_ids)}", "interactive", "Save",
                                    None, None, reply_id)


def test_two_saves_of_the_same_prefix_at_once_get_different_codes(sc):
    [(_, first)] = draft_once(sc, [ST["ST-01"][1]], text(ST["ST-01"][0]))
    [(_, second)] = draft_once(sc, [ST["ST-01"][1]], text(ST["ST-01"][0] + " corner"))
    turn, gaz = staff_turn(sc), sc.cache.get()  # read on the fixture's connection, first
    barrier, results = threading.Barrier(2, timeout=30), {}

    def save(name, card):
        with psycopg.connect(sc.app_url, autocommit=True) as conn:
            ctx = staff_actions.Ctx(conn, turn, gaz)
            barrier.wait()
            [reply] = staff_actions.respond(ctx, [tap_message(button(card, "draft:save:"))])
            results[name] = body(reply)

    threads = [threading.Thread(target=save, args=(n, c), daemon=True)
               for n, c in (("a", first), ("b", second))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(results.values()) == ["✅ Saved as *DH6-109*. Listing ab active hai.",
                                        "✅ Saved as *DH6-110*. Listing ab active hai."]


def test_another_agencys_draft_cannot_be_tapped(sc):
    other = create_agency(sc.conn, "Other Realty (DEMO)", "other", "100000000000002")
    with tenant_transaction(sc.conn, other):
        staff = sc.conn.execute(
            "INSERT INTO staff (agency_id, name, whatsapp_number, role) VALUES "
            "(current_agency_id(), 'Other (DEMO)', '15550000999', 'agent') RETURNING id"
        ).fetchone()[0]
        draft = sc.conn.execute(
            "INSERT INTO listing_draft (agency_id, staff_id, kind, payload) VALUES "
            "(current_agency_id(), %s, 'create', '{}'::jsonb) RETURNING id", (staff,)
        ).fetchone()[0]
    [(_, closed)] = sc.send(tap(f"draft:save:{draft}:1"))
    assert body(closed) == NOTICES["staff_card_closed"]
    with tenant_transaction(sc.conn, other):
        assert sc.conn.execute("SELECT status FROM listing_draft").fetchall() == [("open",)]


def test_the_staff_card_map_is_tenant_scoped_append_only_and_has_one_target(sc):
    scenario_4a(sc)
    draft, prop = sc.query("SELECT d.id, p.id FROM listing_draft d, property p "
                           "WHERE p.listing_code = 'DH6-109'")[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        sc.query("INSERT INTO staff_card (agency_id, dedupe_key, listing_draft_id, "
                 "property_id) VALUES (current_agency_id(), 'k1', %s, %s) RETURNING id",
                 (draft, prop))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        sc.query("UPDATE staff_card SET dedupe_key = 'x' RETURNING id")
    other = create_agency(sc.conn, "Other Realty (DEMO)", "other", "100000000000002")
    with tenant_transaction(sc.conn, other):
        assert sc.conn.execute("SELECT count(*) FROM staff_card").fetchone() == (0,)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            sc.conn.execute("INSERT INTO staff_card (agency_id, dedupe_key, property_id) "
                            "VALUES (current_agency_id(), 'k2', %s)", (prop,))
    with psycopg.connect(sc.admin_url, autocommit=True) as admin:
        assert admin.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                             "WHERE oid = 'staff_card'::regclass").fetchone() == (True, True)


def test_a_draft_card_reaches_its_draft_from_the_quoted_wamid(sc):
    [(wamid, _)] = draft_once(sc, [ST["ST-07"][1]], text(ST["ST-07"][0]))
    draft = sc.query("SELECT id FROM listing_draft")[0][0]
    with tenant_transaction(sc.conn, sc.agency):
        assert staff_actions.card_refs(sc.conn, {wamid, "wamid.unknown"}) == {
            wamid: (draft, None)}
    assert staff_tools.MEDIA_COLUMNS[0] == "id"
