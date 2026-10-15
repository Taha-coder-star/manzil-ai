"""Task 7.4 acceptance tests: the staff buffer (Red-Team R-08 / R-09 / R-10).

Done condition (Execution Plan 7.4): text + 3 photos -> 1 segment with 3 photos; two
listings back-to-back -> 2 segments; a photo-only segment is flagged. Plus: done / bas,
stale timers, replays, a restarted worker, missing photos, commands, tenant and staff
isolation. The classifier is checked on all 20 staff benchmark items.

Database tests use the restricted manzil_app login (RLS on); the WhatsApp channel and
the bucket are fakes (tests/test_media.py) and all data is fictional. Quiet windows
are not waited for: due() moves the pending jobs' available_at to now.
"""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from app import handoff_actions, orchestrator, staff_buffer, webhook, whatsapp
from app.db import tenant_transaction
from app.handlers import make_handlers
from app.ingest import store_event
from app.provision_agency import create_agency
from app.worker import Worker
from tests.test_media import FakeChannel, FakeStorage, photo

# Task 11.2: what a broker gets for anything that is not a lead command (R-15).
BROKER_HELP = handoff_actions.TEXT["help"] + "\n\n" + handoff_actions.TEXT["none_open"]

PNID_A, PNID_B = "100000000000001", "100000000000002"
AGENT, AGENT_2, BROKER, CUSTOMER = (
    "923000000011", "923000000012", "923000000013", "923000000001")
BENCH = Path(__file__).resolve().parents[1] / "bench" / "staff.json"
LISTING = "New house DHA Phase 6, 500 yard, 4 bed 5 bath, demand 8 crore, available"
LISTING_2 = "Shop for rent in Clifton block 5, 350 sq ft, 2.5 lakh per month"
PHOTO_ONLY = whatsapp.text(
    "Ye photos kis listing ki hain? Listing ki tafseelat likh kar bhejein, ya listing "
    "ke card ko reply karke photos bhejein.")
MISSING = whatsapp.text("Kuch photos save nahi ho sakin. Please woh photos dobara bhejein.")


# --- classifier (offline) -------------------------------------------------------------

@pytest.mark.parametrize("item", json.loads(BENCH.read_text(encoding="utf-8")),
                         ids=lambda item: item["id"])
def test_every_staff_benchmark_item_is_classified(item):
    expected = "command" if item["category"].startswith("update") else "description"
    assert staff_buffer.classify_text(item["input"]) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("done", "done"), ("Bas!", "done"), (" DONE. ", "done"), ("بس", "done"),
    ("bas itna", "note"),  # only the whole message closes a segment
    ("photos neeche hain", "note"), ("ok", "note"), ("ghar DHA 6 mein", "note"),
    ("#undo DH6-109", "command"), ("#done L-12", "command"), ("DH6-102 ka price?", "command"),
    ("Bahria wale 250 gaz ghar ka price 3 crore kar do", "command"),
    ("corner wala naya ghar DHA 6, 500 gaz, 8 crore", "description"),  # new listing
    ("3 bed flat, 1800 sq ft, 4.5 crore", "description"),
    ("", None), ("   ", None), (None, None),
])
def test_classify_text(text, expected):
    assert staff_buffer.classify_text(text) == expected


def test_quiet_window_is_inside_the_planned_range():
    assert 60 <= staff_buffer.QUIET_SECONDS <= 90


# --- database ---------------------------------------------------------------------------

@pytest.fixture
def sb(app_role_db):
    _, app_url, _ = app_role_db
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, "Demo Realty (DEMO)", "demo", PNID_A)
    other = create_agency(conn, "Other Realty (DEMO)", "other", PNID_B)
    for agency_id in (agency, other):
        with tenant_transaction(conn, agency_id):
            for name, number, role in [("Agent (DEMO)", AGENT, "agent"),
                                       ("Agent 2 (DEMO)", AGENT_2, "admin"),
                                       ("Ahmed (DEMO)", BROKER, "broker")]:
                conn.execute(
                    "INSERT INTO staff (agency_id, name, whatsapp_number, role) "
                    "VALUES (current_agency_id(), %s, %s, %s)", (name, number, role))
    channel, storage = FakeChannel(), FakeStorage()
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket="demo-bucket",
        s3_region="us-east-1", s3_endpoint_url=None, media_public_base_url=None,
        site_address="demo.example",
    )
    handlers = make_handlers(settings, client_factory=lambda pnid: channel, storage=storage,
                             image_link=lambda: "https://demo.example/x.jpg")
    agencies = {PNID_A: agency, PNID_B: other}
    state = SimpleNamespace(worker=None, clock=int(time.time()))

    def new_worker():
        if state.worker is not None:
            state.worker.close()
        state.worker = Worker(app_url, [agency, other], handlers, threading.Event(),
                              backoff=lambda n: 0)

    new_worker()

    def deliver(*messages, pnid=PNID_A):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": pnid}, "messages": list(messages),
        }}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(conn, agencies[pnid]):
            for event in events:
                store_event(conn, agencies[pnid], event)

    def drain():
        while state.worker.run_once():
            pass

    def due():
        """The quiet window (and media waits) have passed."""
        for agency_id in (agency, other):
            with tenant_transaction(conn, agency_id):
                conn.execute("UPDATE job SET available_at = now() WHERE status = 'pending'")
        drain()

    def query(sql, params=(), *, agency_id=agency):
        with tenant_transaction(conn, agency_id):
            return conn.execute(sql, params).fetchall()

    def tick():
        state.clock += 1
        return state.clock

    def text(wamid, body, sender=AGENT):
        return {"from": sender, "id": wamid, "timestamp": str(tick()), "type": "text",
                "text": {"body": body}}

    def image(wamid, caption=None, sender=AGENT, *, data=True):
        media_id = f"m-{wamid}"
        if data:
            channel.files[media_id] = photo(size=(400 + len(channel.files), 300))
        content = {"id": media_id, "mime_type": "image/jpeg", "sha256": "ignored"}
        if caption:
            content["caption"] = caption
        return {"from": sender, "id": wamid, "timestamp": str(tick()), "type": "image",
                "image": content}

    yield SimpleNamespace(
        conn=conn, agency=agency, other=other, channel=channel, deliver=deliver,
        drain=drain, due=due, query=query, text=text, image=image, new_worker=new_worker,
        state=state,
    )
    state.worker.close()
    conn.close()


def segments(sb, **kwargs):
    """[(status, kind, flush_reason, flags, [(wamid, text_role, is_photo), ...])]"""
    rows = sb.query(
        "SELECT id, status, kind, flush_reason, flags FROM staff_segment ORDER BY created_at",
        **kwargs)
    out = []
    for segment_id, status, kind, reason, flags in rows:
        items = sb.query(
            "SELECT m.provider_message_id, i.text_role, i.is_photo FROM staff_segment_item i "
            "JOIN message m ON m.id = i.message_id WHERE i.segment_id = %s "
            "ORDER BY i.position", (segment_id,), **kwargs)
        out.append((status, kind, reason, flags, items))
    return out


def segment_turns(sb):
    return sb.query(
        "SELECT route, status, message_count FROM conversation_turn "
        "WHERE staff_segment_id IS NOT NULL ORDER BY created_at")


def stub(texts, photos):
    return whatsapp.text(f"Listing segment (stub) · {texts} text(s), {photos} photo(s) · "
                         "drafts come with Task 8.2")


def test_text_and_three_photos_are_one_segment(sb):
    sb.deliver(sb.text("wamid.T1", LISTING))
    sb.deliver(sb.image("wamid.P1"), sb.image("wamid.P2"), sb.image("wamid.P3"))
    sb.drain()

    assert sb.channel.sent == []  # buffered: nothing answered per message
    [(status, kind, reason, flags, items)] = segments(sb)
    assert (status, kind, reason, flags) == ("open", None, None, [])
    assert items == [("wamid.T1", "description", False), ("wamid.P1", None, True),
                     ("wamid.P2", None, True), ("wamid.P3", None, True)]

    sb.due()
    assert segments(sb)[0][:4] == ("flushed", "description", "timer", [])
    assert sb.channel.sent == [stub(1, 3)]
    assert segment_turns(sb) == [("staff", "replied", 4)]


def test_two_listings_back_to_back_are_two_segments(sb):
    sb.deliver(sb.text("wamid.T1", LISTING), sb.image("wamid.P1"), sb.image("wamid.P2"),
               sb.text("wamid.T2", LISTING_2), sb.image("wamid.P3"))
    sb.drain()

    first, second = segments(sb)
    assert first == ("flushed", "description", "next_description", [], [
        ("wamid.T1", "description", False), ("wamid.P1", None, True),
        ("wamid.P2", None, True)])
    assert second == ("open", None, None, [], [
        ("wamid.T2", "description", False), ("wamid.P3", None, True)])
    assert sb.channel.sent == [stub(1, 2)]  # the first is answered at once

    sb.due()
    assert sb.channel.sent == [stub(1, 2), stub(1, 1)]


def test_a_second_listing_in_a_later_turn_closes_the_first(sb):
    sb.deliver(sb.text("wamid.T1", LISTING), sb.image("wamid.P1"))
    sb.drain()
    sb.deliver(sb.text("wamid.T2", LISTING_2))
    sb.drain()

    assert [s[:3] for s in segments(sb)] == [
        ("flushed", "description", "next_description"), ("open", None, None)]
    sb.due()
    # The first segment's timer is stale (it was closed): one reply per segment.
    assert sb.channel.sent == [stub(1, 1), stub(1, 0)]


def test_photos_before_the_text_attach_to_the_next_description(sb):
    sb.deliver(sb.image("wamid.P1"), sb.image("wamid.P2"))
    sb.drain()
    sb.deliver(sb.text("wamid.T1", LISTING))
    sb.drain()
    sb.due()

    [(status, kind, _, flags, items)] = segments(sb)
    assert (status, kind, flags) == ("flushed", "description", [])
    assert items == [("wamid.P1", None, True), ("wamid.P2", None, True),
                     ("wamid.T1", "description", False)]
    assert sb.channel.sent == [stub(1, 2)]


def test_a_photo_only_segment_is_flagged_and_asked_about(sb):
    sb.deliver(sb.image("wamid.P1"), sb.image("wamid.P2", caption="ye dekhein"))
    sb.drain()
    sb.due()

    [(status, kind, reason, flags, items)] = segments(sb)
    assert (status, kind, reason, flags) == ("flushed", "photo_only", "timer",
                                             ["photo_only"])
    assert items == [("wamid.P1", None, True), ("wamid.P2", "note", True)]
    assert sb.channel.sent == [PHOTO_ONLY]


def test_a_captioned_photo_is_a_description(sb):
    sb.deliver(sb.image("wamid.P1", caption=LISTING), sb.image("wamid.P2"))
    sb.drain()
    sb.due()

    [(_, kind, _, _, items)] = segments(sb)
    assert kind == "description"
    assert items == [("wamid.P1", "description", True), ("wamid.P2", None, True)]


@pytest.mark.parametrize("word", ["done", "Bas"])
def test_done_closes_the_segment_once(sb, word):
    sb.deliver(sb.text("wamid.T1", LISTING), sb.image("wamid.P1"))
    sb.drain()
    sb.deliver(sb.text("wamid.D1", word))
    sb.drain()
    assert segments(sb)[0][:3] == ("flushed", "description", "done")
    assert sb.channel.sent == [stub(1, 1)]

    sb.deliver(sb.text("wamid.D2", word))  # nothing open: ignored
    sb.drain()
    sb.due()
    assert sb.channel.sent == [stub(1, 1)]
    assert len(segments(sb)) == 1
    assert segment_turns(sb) == [("staff", "replied", 2)]


def test_a_newer_item_makes_the_older_timer_stale(sb):
    sb.deliver(sb.text("wamid.T1", LISTING))
    sb.drain()
    sb.deliver(sb.image("wamid.P1"))
    sb.drain()
    flushes = sb.query(
        "SELECT payload->>'version' FROM job WHERE job_type = %s ORDER BY created_at",
        (staff_buffer.FLUSH_JOB,))
    assert flushes == [("2",), ("3",)]

    sb.due()
    assert sb.query(
        "SELECT status FROM job WHERE job_type = %s ORDER BY created_at",
        (staff_buffer.FLUSH_JOB,)) == [("succeeded",), ("succeeded",)]
    assert sb.channel.sent == [stub(1, 1)]  # the stale timer did nothing


def test_replayed_webhooks_and_jobs_change_nothing(sb):
    messages = [sb.text("wamid.T1", LISTING), sb.image("wamid.P1")]
    sb.deliver(*messages)
    sb.drain()
    sb.deliver(*messages)  # WhatsApp redelivers
    sb.drain()
    sb.due()
    with tenant_transaction(sb.conn, sb.agency):
        # A replayed answer job (another dedupe key) finds the segment answered.
        from app.queue import enqueue_job
        segment_id = sb.conn.execute("SELECT id FROM staff_segment").fetchone()[0]
        enqueue_job(sb.conn, staff_buffer.ANSWER_JOB, "replay-answer",
                    {"segment_id": str(segment_id), "waits": 0}, conversation_key=AGENT)
    sb.drain()

    [(_, _, _, _, items)] = segments(sb)
    assert [i[0] for i in items] == ["wamid.T1", "wamid.P1"]
    assert sb.channel.sent == [stub(1, 1)]
    assert segment_turns(sb) == [("staff", "replied", 2)]


def test_the_segment_survives_a_worker_restart(sb):
    sb.deliver(sb.text("wamid.T1", LISTING), sb.image("wamid.P1"))
    sb.drain()
    sb.new_worker()
    sb.deliver(sb.image("wamid.P2"))
    sb.drain()
    sb.due()

    [(status, _, _, _, items)] = segments(sb)
    assert status == "flushed" and len(items) == 3
    assert sb.channel.sent == [stub(1, 2)]


def test_a_crashed_segment_turn_is_resumed_and_answered_once(sb, monkeypatch):
    sb.deliver(sb.text("wamid.T1", LISTING))
    sb.drain()
    calls = []

    def crash_once(turn):
        calls.append(turn.id)
        if len(calls) == 1:
            raise RuntimeError("fictional crash")
        return [stub(1, 0)]

    monkeypatch.setattr(orchestrator, "segment_reply", crash_once)
    sb.due()
    sb.due()  # the retry resumes the same open turn

    assert len(set(calls)) == 1 and len(calls) == 2
    assert sb.channel.sent == [stub(1, 0)]
    assert sb.query("SELECT status, attempts FROM conversation_turn "
                    "WHERE staff_segment_id IS NOT NULL") == [("replied", 2)]


def test_photos_still_downloading_wait_then_are_flagged(sb):
    sb.state.worker.handlers.pop("fetch_media")  # the download never runs: pending
    sb.deliver(sb.text("wamid.T1", LISTING), sb.image("wamid.P1"))
    sb.drain()
    sb.due()   # timer -> answer waits (1)
    sb.due()   # waits (2)
    sb.due()   # answers, flagged

    assert segments(sb)[0][3] == ["media_pending"]
    assert sb.channel.sent == [stub(1, 1), MISSING]


def test_a_failed_photo_is_flagged_not_dropped(sb):
    sb.deliver(sb.text("wamid.T1", LISTING), sb.image("wamid.P1", data=False))
    sb.channel.media_errors = [whatsapp.PermanentMediaError("http_404")]
    sb.drain()
    sb.due()

    [(_, _, _, flags, items)] = segments(sb)
    assert flags == ["media_failed"]
    assert [i[0] for i in items] == ["wamid.T1", "wamid.P1"]
    assert sb.channel.sent == [stub(1, 1), MISSING]


def test_a_command_is_answered_at_once_and_leaves_the_segment_open(sb):
    sb.deliver(sb.text("wamid.T1", LISTING), sb.image("wamid.P1"),
               sb.text("wamid.C1", "#DH6-102 price 7.8 crore"))
    sb.drain()

    assert segments(sb)[0][0] == "open"
    assert sb.channel.sent == [whatsapp.text("Staff mode (stub) · 1 message in this turn")]
    sb.due()
    assert sb.channel.sent[1:] == [stub(1, 1)]


def test_the_unsupported_message_of_a_photo_album_is_ignored(sb):
    # Seen live 2026-10-13: an album of 3 photos also delivers a type "unsupported"
    # message (same second, its own turn), which must not get a reply of its own.
    sb.deliver(sb.text("wamid.T1", LISTING))
    sb.drain()
    album = {"from": AGENT, "id": "wamid.U1", "timestamp": str(int(time.time())),
             "type": "unsupported",
             "errors": [{"code": 131051, "title": "Message type unknown"}]}
    sb.deliver(album)
    sb.drain()
    sb.deliver(sb.image("wamid.P1"), sb.image("wamid.P2"), sb.image("wamid.P3"))
    sb.drain()
    reaction = {"from": AGENT, "id": "wamid.R1", "timestamp": str(int(time.time())),
                "type": "reaction", "reaction": {"message_id": "wamid.OUT1", "emoji": "👍"}}
    sb.deliver(reaction)
    sb.drain()
    assert sb.channel.sent == []

    sb.due()
    [(_, kind, _, _, items)] = segments(sb)
    assert kind == "description" and [i[0] for i in items] == [
        "wamid.T1", "wamid.P1", "wamid.P2", "wamid.P3"]
    assert sb.channel.sent == [stub(1, 3)]


def test_a_note_without_a_segment_is_a_normal_staff_turn(sb):
    sb.deliver(sb.text("wamid.N1", "salaam"))
    sb.drain()

    assert segments(sb) == []
    assert sb.channel.sent == [whatsapp.text("Staff mode (stub) · 1 message in this turn")]


def test_a_note_joins_the_open_segment(sb):
    sb.deliver(sb.text("wamid.T1", LISTING), sb.text("wamid.N1", "photos neeche hain"),
               sb.image("wamid.P1"))
    sb.drain()
    sb.due()

    [(_, _, _, _, items)] = segments(sb)
    assert items == [("wamid.T1", "description", False), ("wamid.N1", "note", False),
                     ("wamid.P1", None, True)]
    assert sb.channel.sent == [stub(2, 1)]


def test_staff_members_and_agencies_never_share_a_segment(sb):
    sb.deliver(sb.text("wamid.T1", LISTING), sb.image("wamid.P1"))
    sb.deliver(sb.image("wamid.P2", sender=AGENT_2))
    sb.deliver(sb.image("wamid.P3"), pnid=PNID_B)  # same phone, other agency
    sb.drain()
    sb.due()

    assert [(s[1], [i[0] for i in s[4]]) for s in segments(sb)] == [
        ("description", ["wamid.T1", "wamid.P1"]), ("photo_only", ["wamid.P2"])]
    assert [(s[1], [i[0] for i in s[4]]) for s in segments(sb, agency_id=sb.other)] == [
        ("photo_only", ["wamid.P3"])]


def test_customer_and_broker_turns_are_not_buffered(sb):
    sb.deliver(sb.text("wamid.C1", LISTING, sender=CUSTOMER),
               sb.text("wamid.B1", LISTING, sender=BROKER))
    sb.drain()

    assert segments(sb) == []
    assert sorted(p["text"]["body"] for p in sb.channel.sent) == [
        "Customer mode (stub) · 1 message in this turn",
        BROKER_HELP,   # Task 11.2: the lead-command help, never extraction (R-15)
    ]
