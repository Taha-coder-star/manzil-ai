"""The demo-data reset against a migrated, disposable schema. All data is fictional; every
write and check runs as the restricted manzil_app login."""

import datetime
import uuid

import psycopg
import pytest

from app import demo_reset, seed
from app.db import tenant_transaction
from app.provision_agency import create_agency
from app.queue import enqueue_job
from app.staff_actions import _allocate_code
from tests.test_seed import ENV

CUSTOMER = "15550000999"
SHA = "a" * 64


@pytest.fixture
def db(app_role_db):
    admin_url, app_url, _ = app_role_db
    with psycopg.connect(app_url, autocommit=True) as conn:
        agency = create_agency(conn, seed.DEMO_AGENCY_NAME, "demo-" + uuid.uuid4().hex,
                               uuid.uuid4().hex)
    result = seed.run(agency, admin_url, app_url, ENV, apply=True)
    assert not result.conflicts
    return admin_url, app_url, agency


def one(db, query, *params):
    """Run one statement in its own tenant transaction; its first row, if it returns any."""
    _, app_url, agency = db
    with psycopg.connect(app_url, autocommit=True) as conn, tenant_transaction(conn, agency):
        cursor = conn.execute(query, params)
        return cursor.fetchone() if cursor.description else None


def listing(db, code):
    return one(db, "SELECT id, status, price_pkr, version, last_confirmed_at, created_at, "
                   "cover_media_id FROM property WHERE listing_code = %s", code)


def conversation(db, wa_id):
    return one(db, "INSERT INTO conversation (agency_id, customer_wa_id, "
                   "last_customer_message_at) VALUES (current_agency_id(), %s, now()) "
                   "RETURNING id", wa_id)[0]


def add_test_listing(db, code="DH6-109"):
    source = listing(db, "DH6-101")
    return one(db, """
        INSERT INTO property (agency_id, listing_code, status, purpose, property_type, category,
                              location_id, price_pkr, area_value, area_unit, bedrooms, is_demo)
        SELECT agency_id, %s, 'active', purpose, property_type, category, location_id,
               price_pkr, area_value, area_unit, bedrooms, TRUE FROM property WHERE id = %s
        RETURNING id
        """, code, source[0])[0]


def edit_like_staff(db, code, **values):
    """The update app.staff_actions._apply makes, with its event."""
    row = listing(db, code)
    sets = "".join(f"{column} = %s, " for column in values)
    one(db, f"UPDATE property SET {sets}version = version + 1, updated_at = now(), "
            "last_confirmed_at = now() WHERE id = %s", *values.values(), row[0])
    one(db, "INSERT INTO property_event (agency_id, property_id, event_type, from_version, "
            "to_version, changes) VALUES (current_agency_id(), %s, 'updated', %s, %s, '{}')",
        row[0], row[3], row[3] + 1)
    return row


def customer_history(db):
    """A customer who searched, saw a listing, got its photo and asked for an agent."""
    conversation_id = conversation(db, CUSTOMER)
    property_id = listing(db, "DH6-102")[0]
    media_id = one(db, "INSERT INTO property_media (agency_id, property_id, original_key, "
                       "sort_order) VALUES (current_agency_id(), %s, 'loaded/original', 50) "
                       "RETURNING id", property_id)[0]
    one(db, "INSERT INTO requirement_state (agency_id, conversation_id, version) "
            "VALUES (current_agency_id(), %s, 1)", conversation_id)
    one(db, "INSERT INTO requirement_patch_log (agency_id, conversation_id, version, ops) VALUES "
            "(current_agency_id(), %s, 1, '[{\"slot\": \"purpose\", \"op\": \"set\"}]')",
        conversation_id)
    one(db, "INSERT INTO media_sent (agency_id, conversation_id, property_id, property_media_id) "
            "VALUES (current_agency_id(), %s, %s, %s)", conversation_id, property_id, media_id)
    run_id = one(db, "INSERT INTO search_run (agency_id, conversation_id, requirement_version, "
                     "filters, total_matches) VALUES (current_agency_id(), %s, 1, '{}', 1) "
                     "RETURNING id", conversation_id)[0]
    one(db, "INSERT INTO shown_item (agency_id, conversation_id, search_run_id, position, "
            "property_id) VALUES (current_agency_id(), %s, %s, 1, %s) RETURNING id",
        conversation_id, run_id, property_id)
    lead_id = one(db, "INSERT INTO lead (agency_id, conversation_id, lead_number) "
                      "VALUES (current_agency_id(), %s, next_lead_number()) RETURNING id",
                  conversation_id)[0]
    handoff_id = one(db, "INSERT INTO handoff (agency_id, conversation_id, lead_id) "
                         "VALUES (current_agency_id(), %s, %s) RETURNING id",
                     conversation_id, lead_id)[0]
    return conversation_id, lead_id, handoff_id


def attach_staff_photo(db, code):
    """A staff WhatsApp photo on a listing that had no cover, which it then becomes."""
    staff_id, number = one(db, "SELECT id, whatsapp_number FROM staff WHERE role = 'agent'")
    conversation_id = conversation(db, number)
    message_id = one(db, "INSERT INTO message (agency_id, conversation_id, direction, "
                         "message_type) VALUES (current_agency_id(), %s, 'inbound', 'image') "
                         "RETURNING id", conversation_id)[0]
    inbound_id = one(db, "INSERT INTO inbound_media (agency_id, conversation_id, message_id, "
                         "staff_id, wa_media_id) VALUES (current_agency_id(), %s, %s, %s, 'wa-1') "
                         "RETURNING id", conversation_id, message_id, staff_id)[0]
    property_id = listing(db, code)[0]
    media_id = one(db, "INSERT INTO property_media (agency_id, property_id, original_key, "
                       "sort_order, source_inbound_media_id, content_sha256) VALUES "
                       "(current_agency_id(), %s, 'staff/original', 0, %s, %s) RETURNING id",
                   property_id, inbound_id, SHA)[0]
    one(db, "UPDATE property SET cover_media_id = %s WHERE id = %s", media_id, property_id)
    return media_id


def reset(db, apply=True):
    _, app_url, agency = db
    return demo_reset.run(agency, app_url, apply=apply)


def seed_plan(db):
    admin_url, app_url, agency = db
    return seed.run(agency, admin_url, app_url, ENV, apply=False)


# --- no database ---------------------------------------------------------------

def test_bad_agency_id_and_missing_url_fail_before_connecting():
    with pytest.raises(seed.SeedError, match="UUID"):
        demo_reset.run("not-a-uuid", "x")
    with pytest.raises(seed.SeedError, match="DATABASE_URL is not set"):
        demo_reset.run(str(uuid.uuid4()), "")


def test_connection_failure_does_not_leak_the_url(capsys):
    secret = "s3cret-" + uuid.uuid4().hex
    code = demo_reset.main(["--agency-id", str(uuid.uuid4())], {
        "DATABASE_URL": f"postgresql://manzil:{secret}@127.0.0.1:1/nowhere"})
    assert code == 1 and secret not in capsys.readouterr().out


# --- the reset -----------------------------------------------------------------

def test_a_freshly_seeded_agency_needs_nothing(db):
    before = one(db, "SELECT array_agg(version ORDER BY listing_code), max(updated_at) "
                     "FROM property")
    result = reset(db)
    assert result.outcome == "committed" and not result.retired and not result.restored
    assert one(db, "SELECT array_agg(version ORDER BY listing_code), max(updated_at) "
                   "FROM property") == before


def test_dry_run_plans_everything_and_writes_nothing(db):
    add_test_listing(db)
    edited = edit_like_staff(db, "DH6-102", price_pkr=78_000_000)
    conversation_id, _, _ = customer_history(db)
    result = reset(db, apply=False)
    assert result.outcome.startswith("planned")
    assert result.retired == [("DH6-109", "TEST-101")]
    assert result.restored == {"DH6-102": ["last_confirmed_at", "price_pkr"]}
    assert (result.conversations, result.leads, result.handoffs) == (1, 1, 1)
    assert listing(db, "DH6-109") is not None
    assert listing(db, "DH6-102")[3] == edited[3] + 1
    assert one(db, "SELECT customer_wa_id FROM conversation WHERE id = %s",
               conversation_id)[0] == CUSTOMER


def test_listings_go_back_and_the_next_code_is_dh6_109_again(db):
    original = listing(db, "DH6-102")
    sold = listing(db, "DH6-104")
    test_id = add_test_listing(db)
    edit_like_staff(db, "DH6-102", price_pkr=78_000_000)
    edit_like_staff(db, "DH6-104", status="sold")
    events = one(db, "SELECT count(*) FROM property_event")[0]

    result = reset(db)
    assert result.outcome == "committed"

    restored = listing(db, "DH6-102")
    assert restored[2] == original[2] and restored[4] == original[4]
    assert listing(db, "DH6-104")[1] == "active" and listing(db, "DH6-104")[4] == sold[4]
    assert one(db, "SELECT listing_code, status, internal_notes FROM property WHERE id = %s",
               test_id) == ("TEST-101", "withdrawn", "Retired by the demo reset; was DH6-109.")

    _, app_url, agency = db
    with psycopg.connect(app_url, autocommit=True) as conn, tenant_transaction(conn, agency):
        assert _allocate_code(conn, agency, "DH6") == "DH6-109"
    # The existing audit trail stays, and the seed loader sees a clean dataset.
    assert one(db, "SELECT count(*) FROM property_event")[0] == events
    plan = seed_plan(db)
    assert not plan.conflicts and all(not phase.inserts for phase in plan.phases)
    assert one(db, "SELECT count(*) FROM v_public_listing")[0] == 27


def test_an_old_undo_stops_after_a_reset(db):
    """_undo and _undo_ask only act when the listing is at the event's version."""
    edit_like_staff(db, "DH6-102", price_pkr=78_000_000)
    reset(db)
    version, last_event = one(db, """
        SELECT p.version, max(e.to_version) FROM property p
        JOIN property_event e ON e.property_id = p.id
        WHERE p.listing_code = 'DH6-102' GROUP BY p.version""")
    assert version > last_event


def test_retired_codes_keep_counting_and_a_second_reset_changes_nothing(db):
    add_test_listing(db)
    reset(db)
    add_test_listing(db)
    assert reset(db).retired == [("DH6-109", "TEST-102")]
    again = reset(db)
    assert not again.retired and not again.restored and again.conversations == 0


def test_only_photos_staff_attached_are_removed(db):
    loaded = one(db, "INSERT INTO property_media (agency_id, property_id, original_key, "
                     "sort_order) VALUES (current_agency_id(), %s, 'loaded/original', 1) "
                     "RETURNING id", listing(db, "DH6-103")[0])[0]
    attached = attach_staff_photo(db, "DH6-103")
    assert listing(db, "DH6-103")[6] == attached
    assert reset(db).photos == 1
    assert one(db, "SELECT id FROM property_media WHERE id = %s", attached) is None
    assert one(db, "SELECT id FROM property_media WHERE id = %s", loaded) is not None
    assert listing(db, "DH6-103")[6] is None


def test_a_customer_starts_clean_and_old_history_stays_on_the_retired_row(db):
    conversation_id, lead_id, handoff_id = customer_history(db)
    result = reset(db)
    assert (result.conversations, result.leads, result.handoffs) == (1, 1, 1)
    assert one(db, "SELECT customer_wa_id, control_state, mode FROM conversation WHERE id = %s",
               conversation_id) == (f"retired-{conversation_id}", "closed", "paused")
    assert one(db, "SELECT status FROM lead WHERE id = %s", lead_id)[0] == "closed"
    assert one(db, "SELECT status FROM handoff WHERE id = %s", handoff_id)[0] == "cancelled"
    assert one(db, "SELECT count(*) FROM shown_item WHERE conversation_id = %s",
               conversation_id)[0] == 1
    # The same upsert as app.handlers: the phone's next message gets a new conversation.
    new_id = conversation(db, CUSTOMER)
    assert new_id != conversation_id
    for table in ("shown_item", "media_sent", "requirement_state", "lead"):
        assert one(db, f"SELECT count(*) FROM {table} WHERE conversation_id = %s", new_id)[0] == 0


def test_staff_and_broker_keep_their_conversations_but_not_their_drafts(db):
    staff_id, number = one(db, "SELECT id, whatsapp_number FROM staff WHERE role = 'agent'")
    broker = one(db, "SELECT whatsapp_number FROM staff WHERE role = 'broker'")[0]
    staff_conversation, broker_conversation = conversation(db, number), conversation(db, broker)
    draft = one(db, "INSERT INTO listing_draft (agency_id, staff_id, kind) VALUES "
                    "(current_agency_id(), %s, 'create') RETURNING id", staff_id)[0]
    one(db, "INSERT INTO staff_session (agency_id, staff_id) VALUES (current_agency_id(), %s) "
            "RETURNING id", staff_id)
    result = reset(db)
    assert (result.drafts, result.sessions, result.conversations) == (1, 1, 0)
    for conversation_id, wa_id in ((staff_conversation, number), (broker_conversation, broker)):
        assert one(db, "SELECT customer_wa_id, control_state, last_customer_message_at "
                       "IS NOT NULL FROM conversation WHERE id = %s",
                   conversation_id) == (wa_id, "ai_active", True)
    assert one(db, "SELECT status FROM listing_draft WHERE id = %s", draft)[0] == "cancelled"
    assert one(db, "SELECT count(*) FROM staff_session")[0] == 0


def test_a_pending_job_stops_the_reset_and_nothing_is_written(db, capsys):
    add_test_listing(db)
    _, app_url, agency = db
    with psycopg.connect(app_url, autocommit=True) as conn, tenant_transaction(conn, agency):
        enqueue_job(conn, "process_message", "reset-test", {})
    code = demo_reset.main(["--agency-id", str(agency), "--apply"], {"DATABASE_URL": app_url})
    assert code == 2 and "1 process_message job(s)" in capsys.readouterr().out
    assert listing(db, "DH6-109")[1] == "active"


def test_handoff_timers_do_not_stop_the_reset(db):
    _, app_url, agency = db
    with psycopg.connect(app_url, autocommit=True) as conn, tenant_transaction(conn, agency):
        for job_type in demo_reset.NO_WAIT_JOBS:
            enqueue_job(conn, job_type, f"reset-test:{job_type}", {}, delay_seconds=3600)
    result = reset(db)
    assert result.outcome == "committed" and not result.conflicts


def test_a_failed_check_rolls_everything_back(db, monkeypatch):
    add_test_listing(db)
    edit_like_staff(db, "DH6-102", price_pkr=78_000_000)
    original = demo_reset._conversations

    def break_public_count(conn, result, write):
        original(conn, result, write)
        conn.execute("UPDATE property SET status = 'withdrawn' WHERE listing_code = 'DH8-101'")

    monkeypatch.setattr(demo_reset, "_conversations", break_public_count)
    with pytest.raises(seed.SeedError, match="26 public listings, expected 27"):
        reset(db)
    assert listing(db, "DH6-109") is not None
    assert listing(db, "DH6-102")[2] == 78_000_000


def test_an_unseeded_agency_is_a_conflict(app_role_db):
    _, app_url, _ = app_role_db
    with psycopg.connect(app_url, autocommit=True) as conn:
        agency = create_agency(conn, seed.DEMO_AGENCY_NAME, "demo-" + uuid.uuid4().hex,
                               uuid.uuid4().hex)
    result = demo_reset.run(agency, app_url, apply=True)
    assert result.outcome == "rolled back: conflicts" and "run app.seed" in result.conflicts[0]


def test_another_agency_is_refused_and_untouched(db):
    _, app_url, _ = db
    with psycopg.connect(app_url, autocommit=True) as conn:
        other = create_agency(conn, "Real Agency", "real-" + uuid.uuid4().hex, uuid.uuid4().hex)
        with tenant_transaction(conn, other):
            conn.execute("INSERT INTO conversation (agency_id, customer_wa_id) "
                         "VALUES (current_agency_id(), %s)", (CUSTOMER,))
    with pytest.raises(seed.SeedError, match="not the demo agency"):
        demo_reset.run(other, app_url, apply=True)

    customer_history(db)
    reset(db)
    with psycopg.connect(app_url, autocommit=True) as conn, tenant_transaction(conn, other):
        assert conn.execute("SELECT customer_wa_id, control_state FROM conversation"
                            ).fetchall() == [(CUSTOMER, "ai_active")]


def test_the_owner_login_is_refused(db):
    admin_url, _, agency = db
    with pytest.raises(seed.SeedError, match="restricted runtime login"):
        demo_reset.run(agency, admin_url, apply=True)


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("inventory", ["seeded", "extra", "project"])
def test_non_demo_inventory_refuses_the_whole_reset(db, apply, inventory):
    extra = add_test_listing(db)
    edit_like_staff(db, "DH6-102", price_pkr=78_000_000)
    customer = conversation(db, CUSTOMER)
    if inventory == "project":
        one(db, "UPDATE project SET is_demo = FALSE")
    else:
        target = listing(db, "DH6-101")[0] if inventory == "seeded" else extra
        one(db, "UPDATE property SET is_demo = FALSE WHERE id = %s", target)
    with pytest.raises(seed.SeedError, match="non-demo .* inventory present"):
        reset(db, apply=apply)
    assert listing(db, "DH6-109")[1] == "active"
    assert listing(db, "DH6-102")[2] == 78_000_000
    assert one(db, "SELECT customer_wa_id FROM conversation WHERE id = %s", customer)[0] == CUSTOMER


def test_inactive_staff_are_reset_as_customers(db):
    staff_id, number = one(db, "SELECT id, whatsapp_number FROM staff WHERE role = 'broker'")
    old = conversation(db, number)
    one(db, "UPDATE staff SET is_active = FALSE WHERE id = %s", staff_id)
    assert reset(db).conversations == 1
    assert one(db, "SELECT customer_wa_id, control_state FROM conversation WHERE id = %s",
               old) == (f"retired-{old}", "closed")
    assert conversation(db, number) != old


def test_output_holds_no_phone_or_url(db, capsys):
    customer_history(db)
    _, app_url, agency = db
    assert demo_reset.main(["--agency-id", str(agency)], {"DATABASE_URL": app_url}) == 0
    out = capsys.readouterr().out
    assert "customer conversations 1" in out
    assert CUSTOMER not in out and "postgresql" not in out


def test_confirmation_times_come_from_the_insert_time(db):
    """DH6-104 keeps its 35 days; the others their insert time."""
    row = listing(db, "DH6-104")
    assert row[5] - row[4] == datetime.timedelta(days=35)
    assert listing(db, "DH6-101")[4] == listing(db, "DH6-101")[5]
