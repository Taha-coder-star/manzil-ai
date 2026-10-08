"""Task 3.4 conversation state and handoff schema: constraints, tenant isolation,
privileges, lead numbering and compatibility with the existing pipeline.

All data is fictional. Tenant checks run as the restricted manzil_app login.
"""

import threading
import time
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app import webhook
from app.db import tenant_transaction
from app.handlers import record_inbound
from app.ingest import store_event
from app.queue import Job

STATE_TABLES = (
    "requirement_state", "requirement_patch_log", "search_run", "shown_item", "focus",
    "media_sent", "pending_clarification", "lead", "lead_property", "handoff",
)
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
EXPECTED_PRIVILEGES = {
    "requirement_state": {"SELECT", "INSERT", "UPDATE"},
    "requirement_patch_log": {"SELECT", "INSERT"},
    "search_run": {"SELECT", "INSERT"},
    "shown_item": {"SELECT", "INSERT"},
    "focus": {"SELECT", "INSERT", "UPDATE"},
    "media_sent": {"SELECT", "INSERT", "UPDATE"},
    "pending_clarification": {"SELECT", "INSERT", "UPDATE"},
    "lead": {"SELECT", "INSERT", "UPDATE"},
    "lead_property": {"SELECT", "INSERT"},
    "handoff": {"SELECT", "INSERT", "UPDATE"},
    # Existing tables keep their 002 grants.
    "agency": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "conversation": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "message": {"SELECT", "INSERT", "UPDATE", "DELETE"},
}
UPDATABLE = tuple(t for t in STATE_TABLES if "UPDATE" in EXPECTED_PRIVILEGES[t])
APPEND_ONLY = tuple(t for t in STATE_TABLES if "UPDATE" not in EXPECTED_PRIVILEGES[t])
SLOTS = (
    "purpose", "category", "property_types", "location", "budget", "previous_budget", "size",
    "beds_min", "baths_min", "construction", "possession", "investment", "soft_prefs",
    "unmapped_prefs", "excluded_property_ids",
)


def insert(conn, table, **row):
    return conn.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, row)),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
        ),
        tuple(row.values()),
    ).fetchone()[0]


def slot(value, source="text", **overrides):
    return {
        "value": value, "raw": str(value), "source": source, "confidence": 0.9,
        "confirmed": False, "updated_at": "2026-10-08T00:00:00Z", **overrides,
    }


def later(hours=1):
    return datetime.now(UTC) + timedelta(hours=hours)


def wa_id():
    return "92" + str(uuid.uuid4().int)[:10]


def next_lead_number(conn):
    return conn.execute("SELECT next_lead_number()").fetchone()[0]


def add_conversation(conn, t, *, full=True):
    """Inside t's tenant transaction: a conversation with its state rows.

    full=False leaves out the one-per-conversation and child rows, so a test can
    add its own.
    """
    c = SimpleNamespace()
    c.conversation = insert(conn, "conversation", agency_id=t.agency, customer_wa_id=wa_id())
    c.message = insert(conn, "message", agency_id=t.agency, conversation_id=c.conversation,
                       direction="inbound", message_type="text",
                       text_body="DHA Phase 6 mein ghar chahiye")
    c.requirement_state = insert(conn, "requirement_state", agency_id=t.agency,
                                 conversation_id=c.conversation, version=1,
                                 slots=Jsonb({"purpose": slot("sale")}))
    c.requirement_patch_log = insert(conn, "requirement_patch_log", agency_id=t.agency,
                                     conversation_id=c.conversation, version=1,
                                     ops=Jsonb([{"op": "set", "slot": "purpose"}]),
                                     source_message_id=c.message)
    c.search_run = insert(conn, "search_run", agency_id=t.agency,
                          conversation_id=c.conversation, requirement_version=1,
                          filters=Jsonb({"purpose": "sale"}), total_matches=1)
    c.lead = insert(conn, "lead", agency_id=t.agency, conversation_id=c.conversation,
                    lead_number=next_lead_number(conn),
                    requirement_version=1, last_customer_message_id=c.message,
                    assigned_staff_id=t.staff)
    if full:
        c.shown_item = insert(conn, "shown_item", agency_id=t.agency,
                              conversation_id=c.conversation, search_run_id=c.search_run,
                              position=1, property_id=t.property, card_message_id=c.message)
        c.focus = insert(conn, "focus", agency_id=t.agency, conversation_id=c.conversation,
                         property_id=t.property, selected_via="position")
        c.media_sent = insert(conn, "media_sent", agency_id=t.agency,
                              conversation_id=c.conversation, property_id=t.property,
                              property_media_id=t.property_media, message_id=c.message)
        c.pending_clarification = insert(conn, "pending_clarification", agency_id=t.agency,
                                         conversation_id=c.conversation, slot="location",
                                         candidates=Jsonb([]), expires_at=later())
        c.lead_property = insert(conn, "lead_property", agency_id=t.agency, lead_id=c.lead,
                                 property_id=t.property)
        c.handoff = insert(conn, "handoff", agency_id=t.agency,
                           conversation_id=c.conversation, lead_id=c.lead,
                           assigned_staff_id=t.staff)
    return c


def seed_tenant(conn):
    """One fictional agency with exactly one row in every state table."""
    t = SimpleNamespace(agency=uuid.uuid4(), phone_number_id=str(uuid.uuid4().int)[:15])
    with tenant_transaction(conn, t.agency):
        insert(conn, "agency", id=t.agency, name="Fictional agency (DEMO)",
               slug=uuid.uuid4().hex, meta_phone_number_id=t.phone_number_id)
        t.staff = insert(conn, "staff", agency_id=t.agency, name="Demo broker",
                         whatsapp_number="demo-" + uuid.uuid4().hex, role="broker")
        t.property = insert(conn, "property", agency_id=t.agency, listing_code="DH6-101",
                            status="withdrawn", is_demo=True)
        t.property_media = insert(conn, "property_media", agency_id=t.agency,
                                  property_id=t.property, original_key="demo/a.jpg",
                                  sort_order=0)
        vars(t).update(vars(add_conversation(conn, t)))
    return t


def fresh(conn, t):
    """Committed parents for new rows of tenant t, plus a conversation with no state."""
    with tenant_transaction(conn, t.agency):
        f = add_conversation(conn, t, full=False)
        f.bare = insert(conn, "conversation", agency_id=t.agency, customer_wa_id=wa_id())
    return f


def new_row(table, t, f):
    """A fresh valid row of tenant t built on fresh parents f."""
    return {
        "requirement_state": {"conversation_id": f.bare},
        "requirement_patch_log": {"conversation_id": f.conversation, "version": 2,
                                  "ops": Jsonb([{"op": "clear", "slot": "budget"}]),
                                  "source_message_id": f.message},
        "search_run": {"conversation_id": f.conversation, "requirement_version": 1,
                       "filters": Jsonb({}), "total_matches": 0},
        "shown_item": {"conversation_id": f.conversation, "search_run_id": f.search_run,
                       "position": 1, "property_id": t.property,
                       "card_message_id": f.message},
        "focus": {"conversation_id": f.conversation, "property_id": t.property,
                  "selected_via": "quote"},
        "media_sent": {"conversation_id": f.conversation, "property_id": t.property,
                       "property_media_id": t.property_media, "message_id": f.message},
        "pending_clarification": {"conversation_id": f.conversation, "slot": "purpose",
                                  "expires_at": later()},
        "lead": {"conversation_id": f.bare, "lead_number": 1000 + uuid.uuid4().int % 10**6},
        "lead_property": {"lead_id": f.lead, "property_id": t.property},
        "handoff": {"conversation_id": f.conversation, "lead_id": f.lead,
                    "assigned_staff_id": t.staff},
    }[table] | {"agency_id": t.agency}


@pytest.fixture
def st(app_role_db):
    admin_url, app_url, schema = app_role_db
    with psycopg.connect(app_url, autocommit=True) as conn:
        a, b = seed_tenant(conn), seed_tenant(conn)
        yield SimpleNamespace(conn=conn, admin_url=admin_url, app_url=app_url, schema=schema,
                              a=a, b=b)


@pytest.fixture
def admin(st):
    with psycopg.connect(st.admin_url, autocommit=True) as conn:
        yield conn


def run(st, statement, params=(), tenant=None):
    with tenant_transaction(st.conn, (tenant or st.a).agency):
        return st.conn.execute(statement, params)


# Privileges and policies ---------------------------------------------------------------------

@pytest.mark.parametrize("relation", sorted(EXPECTED_PRIVILEGES))
def test_exact_runtime_privileges_and_no_public_access(admin, relation):
    granted = {
        privilege for privilege in PRIVILEGES
        if admin.execute("SELECT has_table_privilege('manzil_app', %s, %s)",
                         (relation, privilege)).fetchone()[0]
    }
    assert granted == EXPECTED_PRIVILEGES[relation]
    assert admin.execute(
        "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a "
        "WHERE c.oid = %s::regclass AND a.grantee = 0", (relation,),
    ).fetchone() == (0,)


def test_lead_allocator_is_executable_only_by_the_runtime(admin):
    assert admin.execute(
        "SELECT has_function_privilege('manzil_app', 'next_lead_number()', 'EXECUTE')"
    ).fetchone() == (True,)
    assert admin.execute(
        "SELECT p.prosecdef, p.provolatile, count(a.*) FILTER (WHERE a.grantee = 0) "
        "FROM pg_proc p LEFT JOIN LATERAL aclexplode(p.proacl) a ON true "
        "WHERE p.oid = 'next_lead_number()'::regprocedure GROUP BY p.prosecdef, p.provolatile"
    ).fetchone() == (False, "v", 0)


def test_state_tables_force_rls_with_single_tenant_policy(st, admin):
    tables = list(STATE_TABLES) + ["agency", "conversation", "message"]
    rows = admin.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s "
        "AND c.relname = ANY(%s)", (st.schema, tables),
    ).fetchall()
    assert {r[0]: (r[1], r[2]) for r in rows} == {t: (True, True) for t in tables}
    policies = admin.execute(
        "SELECT tablename, policyname, cmd, roles::text[], qual, with_check FROM pg_policies "
        "WHERE schemaname = %s AND tablename = ANY(%s)", (st.schema, list(STATE_TABLES)),
    ).fetchall()
    assert sorted(p[0] for p in policies) == sorted(STATE_TABLES)
    for _, name, cmd, roles, qual, with_check in policies:
        assert (name, cmd, roles) == ("tenant_isolation", "ALL", ["manzil_app"])
        for expression in (qual, with_check):
            assert "agency_id = current_agency_id()" in expression


@pytest.mark.parametrize("table", STATE_TABLES)
def test_runtime_cannot_delete_or_truncate(st, table):
    for statement in ("DELETE FROM {}", "TRUNCATE {}"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            run(st, sql.SQL(statement).format(sql.Identifier(table)))


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_histories_are_append_only(st, table):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(st, sql.SQL("UPDATE {} SET id = id").format(sql.Identifier(table)))


# Tenant isolation ----------------------------------------------------------------------------

@pytest.mark.parametrize("table", STATE_TABLES)
def test_two_tenants_and_missing_context(st, table):
    query = sql.SQL("SELECT id FROM {}").format(sql.Identifier(table))
    assert st.conn.execute(query).fetchall() == []
    for t in (st.a, st.b):
        assert run(st, query, tenant=t).fetchall() == [(getattr(t, table),)]


def test_unknown_agency_context_sees_nothing(st):
    unknown = SimpleNamespace(agency=uuid.uuid4())
    for table in STATE_TABLES:
        assert run(st, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)),
                   tenant=unknown).fetchone() == (0,)


@pytest.mark.parametrize("table", STATE_TABLES)
@pytest.mark.parametrize("context", ["other", "missing"])
def test_insert_requires_matching_tenant(st, table, context):
    row = new_row(table, st.b, fresh(st.conn, st.b))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        if context == "other":
            with tenant_transaction(st.conn, st.a.agency):
                insert(st.conn, table, **row)
        else:
            insert(st.conn, table, **row)
    with tenant_transaction(st.conn, st.b.agency):
        insert(st.conn, table, **row)


@pytest.mark.parametrize("table", UPDATABLE)
def test_cannot_move_row_to_other_tenant(st, table):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(st, sql.SQL("UPDATE {} SET agency_id = %s WHERE id = %s").format(
            sql.Identifier(table)), (st.b.agency, getattr(st.a, table)))


@pytest.mark.parametrize("table", UPDATABLE)
def test_invisible_rows_cannot_be_updated(st, table):
    statement = sql.SQL("UPDATE {} SET id = id WHERE id = %s").format(sql.Identifier(table))
    with tenant_transaction(st.conn, st.a.agency):
        assert st.conn.execute(statement, (getattr(st.b, table),)).rowcount == 0
        assert st.conn.execute(statement, (getattr(st.a, table),)).rowcount == 1


CROSS_TENANT_REFERENCES = [
    ("requirement_state", "conversation_id", "conversation"),
    ("requirement_patch_log", "conversation_id", "conversation"),
    ("requirement_patch_log", "source_message_id", "message"),
    ("search_run", "conversation_id", "conversation"),
    ("shown_item", "search_run_id", "search_run"),
    ("shown_item", "property_id", "property"),
    ("shown_item", "card_message_id", "message"),
    ("focus", "conversation_id", "conversation"),
    ("focus", "property_id", "property"),
    ("media_sent", "conversation_id", "conversation"),
    ("media_sent", "property_id", "property"),
    ("media_sent", "property_media_id", "property_media"),
    ("media_sent", "message_id", "message"),
    ("pending_clarification", "conversation_id", "conversation"),
    ("pending_clarification", "asked_message_id", "message"),
    ("lead", "conversation_id", "conversation"),
    ("lead", "last_customer_message_id", "message"),
    ("lead", "assigned_staff_id", "staff"),
    ("lead_property", "lead_id", "lead"),
    ("lead_property", "property_id", "property"),
    ("handoff", "lead_id", "lead"),
    ("handoff", "assigned_staff_id", "staff"),
    ("handoff", "accepted_by_staff_id", "staff"),
]


@pytest.mark.parametrize("table,column,parent", CROSS_TENANT_REFERENCES)
def test_cross_tenant_references_rejected_on_insert(st, table, column, parent):
    row = new_row(table, st.a, fresh(st.conn, st.a)) | {column: getattr(st.b, parent)}
    if column == "accepted_by_staff_id":
        now = datetime.now(UTC)
        row |= {"status": "accepted", "requested_at": now, "accepted_at": now}
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        st.conn, st.a.agency
    ):
        insert(st.conn, table, **row)


@pytest.mark.parametrize("table,column,parent", [
    (table, column, parent) for table, column, parent in CROSS_TENANT_REFERENCES
    if table in UPDATABLE and column not in ("conversation_id", "accepted_by_staff_id")
])
def test_cross_tenant_references_rejected_on_update(st, table, column, parent):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        run(st, sql.SQL("UPDATE {} SET {} = %s WHERE id = %s").format(
            sql.Identifier(table), sql.Identifier(column)
        ), (getattr(st.b, parent), getattr(st.a, table)))


def test_every_tenant_reference_is_scoped_by_agency(admin):
    """Catalog guard: a single-column FK between tenant tables would allow cross-tenant links."""
    foreign_keys = admin.execute(
        "SELECT c.conname, c.confrelid::regclass::text, "
        "(SELECT array_agg(a.attname ORDER BY k.n) FROM unnest(c.conkey) WITH ORDINALITY k(att, n) "
        " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.att), "
        "(SELECT array_agg(a.attname ORDER BY k.n) FROM unnest(c.confkey) WITH ORDINALITY k(att, n) "
        " JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.att) "
        "FROM pg_constraint c WHERE c.contype = 'f' AND c.conrelid::regclass::text = ANY(%s)",
        (list(STATE_TABLES),),
    ).fetchall()
    tenant_parents = [fk for fk in foreign_keys if fk[1] != "agency"]
    assert len(tenant_parents) == 24
    for name, _, columns, referenced in tenant_parents:
        assert (columns[0], referenced[0]) == ("agency_id", "agency_id"), name
    # Every state table is tied to its tenant.
    assert {fk[0].split("_agency_id")[0] for fk in foreign_keys if fk[1] == "agency"} == set(
        STATE_TABLES
    )


# Conversation and message extensions ---------------------------------------------------------

def test_existing_ingestion_writes_default_state_and_derived_modality(st):
    """The unchanged webhook -> record_inbound path keeps working on the new columns."""
    a, sender, now = st.a, wa_id(), str(int(time.time()))
    envelope = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": a.phone_number_id},
        "messages": [
            {"from": sender, "id": "wamid.TEXT1", "timestamp": now, "type": "text",
             "text": {"body": "Clifton mein flat chahiye"}},
            {"from": sender, "id": "wamid.VOICE1", "timestamp": now, "type": "audio",
             "audio": {"id": "fictional-media-id", "voice": True}},
        ],
    }}]}]}
    events, _ = webhook.extract_events(envelope)
    with tenant_transaction(st.conn, a.agency):
        for event in events:
            assert store_event(st.conn, a.agency, event)
        event_ids = st.conn.execute(
            "SELECT id FROM inbound_event ORDER BY event_key"
        ).fetchall()
    for (event_id,) in event_ids:
        record_inbound(st.conn, Job(uuid.uuid4(), a.agency, "process_message", {}, sender,
                                    1, 3, event_id))
    assert run(st, "SELECT mode, control_state, preferred_language, last_modality "
                   "FROM conversation WHERE customer_wa_id = %s", (sender,)).fetchone() == (
        "ai", "ai_active", None, None,
    )
    assert dict(run(st, "SELECT provider_message_id, modality FROM message "
                        "WHERE provider_message_id = ANY(%s)",
                    (["wamid.TEXT1", "wamid.VOICE1"],)).fetchall()) == {
        "wamid.TEXT1": "text", "wamid.VOICE1": "voice",
    }


def test_control_state_is_consistent_with_the_mode_gate(st):
    conversation = st.a.conversation

    def update(assignments):
        return run(st, f"UPDATE conversation SET {assignments} WHERE id = %s", (conversation,))

    with pytest.raises(psycopg.errors.CheckViolation):
        update("control_state = 'paused'")
    # A silenced control state may never look AI-enabled to the mode gate.
    for state in ("handoff_pending", "human_owned", "closed"):
        with pytest.raises(psycopg.errors.CheckViolation,
                           match="conversation_control_state_gate_check"):
            update(f"control_state = '{state}'")
    # Existing behaviour: mode alone still silences an ai_active conversation.
    update("mode = 'human'")
    update("control_state = 'handoff_pending'")
    with pytest.raises(psycopg.errors.CheckViolation,
                       match="conversation_control_state_gate_check"):
        update("mode = 'ai'")
    update("control_state = 'closed', mode = 'paused'")
    update("control_state = 'ai_active', mode = 'ai'")


@pytest.mark.parametrize("assignment", [
    "preferred_language = 'french'", "preferred_language = 'English'",
    "last_modality = 'video'",
])
def test_conversation_preference_values(st, assignment):
    with pytest.raises(psycopg.errors.CheckViolation):
        run(st, f"UPDATE conversation SET {assignment} WHERE id = %s", (st.a.conversation,))


def test_conversation_preferences_accept_known_values(st):
    for language in ("english", "roman_urdu", "urdu"):
        run(st, "UPDATE conversation SET preferred_language = %s, last_modality = 'voice' "
                "WHERE id = %s", (language, st.a.conversation))


def new_message(st, **overrides):
    row = {"agency_id": st.a.agency, "conversation_id": st.a.conversation,
           "direction": "inbound", "message_type": "audio"} | overrides
    with tenant_transaction(st.conn, st.a.agency):
        return insert(st.conn, "message", **row)


def test_message_modality_is_derived_and_read_only(st):
    for message_type, modality in (("text", "text"), ("audio", "voice"), ("image", None),
                                   ("interactive", None)):
        message = new_message(st, message_type=message_type)
        assert run(st, "SELECT modality FROM message WHERE id = %s",
                   (message,)).fetchone() == (modality,)
    with pytest.raises(psycopg.errors.GeneratedAlways):
        new_message(st, modality="voice")


def test_transcript_only_on_inbound_voice(st):
    message = new_message(st, transcript="Clifton mein ghar", stt_meta=Jsonb({"vendor": "x"}))
    assert run(st, "SELECT transcript FROM message WHERE id = %s",
               (message,)).fetchone() == ("Clifton mein ghar",)
    for overrides in (
        {"message_type": "text", "transcript": "x"},
        {"direction": "outbound", "transcript": "x"},
        {"message_type": "image", "stt_meta": Jsonb({})},
        {"stt_meta": Jsonb([])},
        {"context_wamid": "  "},
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            new_message(st, **overrides)


# Requirement state and history ---------------------------------------------------------------

def test_one_requirement_state_per_conversation_with_versioned_updates(st):
    with pytest.raises(psycopg.errors.UniqueViolation):
        run(st, "INSERT INTO requirement_state (agency_id, conversation_id) VALUES (%s, %s)",
            (st.a.agency, st.a.conversation))
    # Optimistic concurrency: a stale writer updates nothing.
    statement = ("UPDATE requirement_state SET version = version + 1 "
                 "WHERE id = %s AND version = 1")
    assert run(st, statement, (st.a.requirement_state,)).rowcount == 1
    assert run(st, statement, (st.a.requirement_state,)).rowcount == 0
    with pytest.raises(psycopg.errors.CheckViolation):
        run(st, "UPDATE requirement_state SET version = -1 WHERE id = %s",
            (st.a.requirement_state,))


def test_requirement_slots_accept_the_full_envelope(st):
    slots = {name: slot(name, source=source) for name, source in zip(
        SLOTS, ["text", "voice", "inferred", "default"] * 4, strict=False)}
    slots["budget"] = slot({"min": None, "max": 50_000_000, "period": None}, confirmed=True)
    run(st, "UPDATE requirement_state SET slots = %s WHERE id = %s",
        (Jsonb(slots), st.a.requirement_state))
    run(st, "UPDATE requirement_state SET slots = '{}' WHERE id = %s",
        (st.a.requirement_state,))


@pytest.mark.parametrize("slots", [
    {"colour": slot("red")},
    {"purpose": "sale"},
    {"purpose": [slot("sale")]},
    {"purpose": {"value": "sale", "raw": "sale"}},
    {"purpose": slot("sale", source="guess")},
    {"purpose": slot("sale", source=["text"])},
    {"purpose": slot("sale", confidence=1.5)},
    {"purpose": slot("sale", confidence=-0.1)},
    {"purpose": slot("sale", confidence="high")},
    {"purpose": slot("sale", confirmed="yes")},
    [],
])
def test_requirement_slots_reject_unknown_or_malformed_slots(st, slots):
    with pytest.raises(psycopg.errors.CheckViolation):
        run(st, "UPDATE requirement_state SET slots = %s WHERE id = %s",
            (Jsonb(slots), st.a.requirement_state))


def test_patch_log_versions_are_unique_and_tied_to_the_state(st):
    def patch(conversation, version, message=None):
        run(st, "INSERT INTO requirement_patch_log "
                "(agency_id, conversation_id, version, ops, source_message_id) "
                "VALUES (%s, %s, %s, %s, %s)",
            (st.a.agency, conversation, version, Jsonb([{"op": "add", "slot": "location"}]),
             message))

    patch(st.a.conversation, 2)
    with pytest.raises(psycopg.errors.UniqueViolation):
        patch(st.a.conversation, 2)
    f = fresh(st.conn, st.a)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        patch(f.bare, 1)  # no requirement_state for that conversation
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        patch(st.a.conversation, 3, f.message)  # message of another conversation
    with pytest.raises(psycopg.errors.CheckViolation):
        patch(st.a.conversation, 0)


@pytest.mark.parametrize("ops", [
    [], [{"slot": "budget"}], [{"op": "merge", "slot": "budget"}],
    [{"op": ["set"], "slot": "budget"}], [[{"op": "set", "slot": "budget"}]], ["set"],
    {"op": "set", "slot": "budget"}, [{"op": "set"}], [{"op": "set", "slot": "colour"}],
    [{"op": "set", "slot": ["budget"]}], [{"op": "reset", "slot": "budget"}],
])
def test_patch_ops_must_be_known_operations(st, ops):
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.DataError)):
        run(st, "INSERT INTO requirement_patch_log (agency_id, conversation_id, version, ops) "
                "VALUES (%s, %s, 2, %s)", (st.a.agency, st.a.conversation, Jsonb(ops)))


def test_patch_ops_accept_every_operation(st):
    ops = [{"op": op, "slot": "budget"}
           for op in ("set", "add", "remove", "clear", "relative")]
    run(st, "INSERT INTO requirement_patch_log (agency_id, conversation_id, version, ops) "
            "VALUES (%s, %s, 2, %s)", (st.a.agency, st.a.conversation, Jsonb(ops)))


# Search memory -------------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,error", [
    ({"requirement_version": 99}, psycopg.errors.ForeignKeyViolation),
    ({"relaxations": Jsonb({})}, psycopg.errors.CheckViolation),
    ({"filters": Jsonb([])}, psycopg.errors.CheckViolation),
    ({"total_matches": -1}, psycopg.errors.CheckViolation),
])
def test_search_run_checks(st, overrides, error):
    row = {"agency_id": st.a.agency, "conversation_id": st.a.conversation,
           "requirement_version": 1, "filters": Jsonb({}), "total_matches": 0} | overrides
    with pytest.raises(error), tenant_transaction(st.conn, st.a.agency):
        insert(st.conn, "search_run", **row)


def test_search_run_version_must_belong_to_its_conversation(st):
    f = fresh(st.conn, st.a)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        st.conn, st.a.agency
    ):
        # Version 2 exists only for st.a.conversation.
        insert(st.conn, "requirement_patch_log", agency_id=st.a.agency,
               conversation_id=st.a.conversation, version=2,
               ops=Jsonb([{"op": "set", "slot": "budget"}]))
        insert(st.conn, "search_run", agency_id=st.a.agency, conversation_id=f.conversation,
               requirement_version=2, filters=Jsonb({}), total_matches=0)


def second_property(st):
    with tenant_transaction(st.conn, st.a.agency):
        prop = insert(st.conn, "property", agency_id=st.a.agency, listing_code="DH6-102",
                      status="withdrawn", is_demo=True)
        media = insert(st.conn, "property_media", agency_id=st.a.agency, property_id=prop,
                       original_key="demo/b.jpg", sort_order=0)
    return prop, media


def test_shown_items_are_unique_per_run_and_stay_in_their_conversation(st):
    a = st.a
    other_property, _ = second_property(st)

    def show(**overrides):
        row = {"agency_id": a.agency, "conversation_id": a.conversation,
               "search_run_id": a.search_run, "position": 2,
               "property_id": other_property} | overrides
        with tenant_transaction(st.conn, a.agency):
            insert(st.conn, "shown_item", **row)

    with pytest.raises(psycopg.errors.UniqueViolation):
        show(position=1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        show(property_id=a.property)
    with pytest.raises(psycopg.errors.CheckViolation):
        show(position=0)
    f = fresh(st.conn, a)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        show(card_message_id=f.message)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        show(search_run_id=f.search_run)
    show()


def test_one_focus_per_conversation(st):
    with pytest.raises(psycopg.errors.UniqueViolation):
        run(st, "INSERT INTO focus (agency_id, conversation_id) VALUES (%s, %s)",
            (st.a.agency, st.a.conversation))
    for assignment in ("selected_via = NULL", "property_id = NULL",
                       "selected_via = 'guess'"):
        with pytest.raises(psycopg.errors.CheckViolation):
            run(st, f"UPDATE focus SET {assignment} WHERE id = %s", (st.a.focus,))
    run(st, "UPDATE focus SET property_id = NULL, selected_via = NULL WHERE id = %s",
        (st.a.focus,))


def test_media_sent_once_per_conversation_and_only_for_its_listing(st):
    a = st.a
    other_property, other_media = second_property(st)

    def record(**overrides):
        row = {"agency_id": a.agency, "conversation_id": a.conversation,
               "property_id": a.property, "property_media_id": a.property_media} | overrides
        with tenant_transaction(st.conn, a.agency):
            return insert(st.conn, "media_sent", **row)

    with pytest.raises(psycopg.errors.UniqueViolation):
        record()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        record(property_media_id=other_media)  # photo of another listing
    sent = record(property_id=other_property, property_media_id=other_media)
    # Deleting the photo keeps the record of the send.
    run(st, "DELETE FROM property_media WHERE id = %s", (other_media,))
    assert run(st, "SELECT property_id, property_media_id FROM media_sent WHERE id = %s",
               (sent,)).fetchone() == (other_property, None)


# Clarifications, leads and handoffs ----------------------------------------------------------

def test_one_open_clarification_per_conversation(st):
    a = st.a

    def ask():
        run(st, "INSERT INTO pending_clarification "
                "(agency_id, conversation_id, slot, expires_at) VALUES (%s, %s, 'purpose', %s)",
            (a.agency, a.conversation, later()))

    with pytest.raises(psycopg.errors.UniqueViolation):
        ask()
    run(st, "UPDATE pending_clarification SET status = 'resolved', closed_at = now() "
            "WHERE id = %s", (a.pending_clarification,))
    ask()


@pytest.mark.parametrize("assignment", [
    "closed_at = now()", "status = 'expired'", "slot = 'budget'",
    "candidates = '{}'", "expires_at = created_at", "unanswered_turns = -1",
])
def test_clarification_checks(st, assignment):
    with pytest.raises(psycopg.errors.CheckViolation):
        run(st, f"UPDATE pending_clarification SET {assignment} WHERE id = %s",
            (st.a.pending_clarification,))


def test_one_open_lead_per_conversation_with_agency_unique_numbers(st):
    a, b = st.a, st.b
    # Numbering is per agency: both seeded agencies started at 1.
    for t in (a, b):
        assert run(st, "SELECT lead_number, code FROM lead WHERE id = %s", (t.lead,),
                   tenant=t).fetchone() == (1, "L-01")

    def open_lead(number):
        run(st, "INSERT INTO lead (agency_id, conversation_id, lead_number) VALUES (%s, %s, %s)",
            (a.agency, a.conversation, number))

    with pytest.raises(psycopg.errors.UniqueViolation, match="lead_one_open_idx"):
        open_lead(500)
    with pytest.raises(psycopg.errors.CheckViolation):
        run(st, "UPDATE lead SET status = 'closed' WHERE id = %s", (a.lead,))
    run(st, "UPDATE lead SET status = 'closed', closed_at = now() WHERE id = %s", (a.lead,))
    with pytest.raises(psycopg.errors.UniqueViolation, match="lead_number"):
        open_lead(1)
    open_lead(500)


@pytest.mark.parametrize("number,code", [(7, "L-07"), (10, "L-10"), (123, "L-123")])
def test_lead_code_is_derived_from_the_number(st, number, code):
    f = fresh(st.conn, st.a)
    with tenant_transaction(st.conn, st.a.agency):
        lead = insert(st.conn, "lead", agency_id=st.a.agency, conversation_id=f.bare,
                      lead_number=number + 10_000)
        st.conn.execute("UPDATE lead SET lead_number = %s WHERE id = %s", (number, lead))
        assert st.conn.execute("SELECT code FROM lead WHERE id = %s",
                               (lead,)).fetchone() == (code,)
    with pytest.raises(psycopg.errors.GeneratedAlways), tenant_transaction(
        st.conn, st.a.agency
    ):
        st.conn.execute("UPDATE lead SET code = 'L-99' WHERE id = %s", (lead,))


def test_lead_numbers_are_sequential_per_agency_and_gapless(st):
    first = run(st, "SELECT next_lead_number()").fetchone()[0]
    assert run(st, "SELECT next_lead_number()").fetchone()[0] == first + 1
    # Rolled back allocations are returned.
    with pytest.raises(RuntimeError), tenant_transaction(st.conn, st.a.agency):
        assert next_lead_number(st.conn) == first + 2
        raise RuntimeError("deliberate rollback")
    assert run(st, "SELECT next_lead_number()").fetchone()[0] == first + 2
    # The other agency's counter is untouched.
    assert run(st, "SELECT next_lead_number()", tenant=st.b).fetchone()[0] == 2


def test_lead_allocation_fails_closed_without_tenant_context(st):
    assert next_lead_number(st.conn) is None
    assert run(st, "SELECT next_lead_number()",
               tenant=SimpleNamespace(agency=uuid.uuid4())).fetchone() == (None,)
    # The NULL a context-less allocation returns can never become a lead number.
    f = fresh(st.conn, st.a)
    with pytest.raises(psycopg.errors.NotNullViolation), tenant_transaction(
        st.conn, st.a.agency
    ):
        insert(st.conn, "lead", agency_id=st.a.agency, conversation_id=f.bare,
               lead_number=None)


def wait_until_lock_wait(admin, pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if admin.execute("SELECT wait_event_type = 'Lock' FROM pg_stat_activity "
                         "WHERE pid = %s", (pid,)).fetchone() == (True,):
            return
        time.sleep(0.05)
    pytest.fail("the second allocation never waited for the first")


def test_concurrent_lead_allocations_are_serialised(st, admin):
    result = {}
    with psycopg.connect(st.app_url, autocommit=True) as first, psycopg.connect(
        st.app_url, autocommit=True
    ) as second:
        def allocate():
            with tenant_transaction(second, st.a.agency):
                result["second"] = next_lead_number(second)

        with tenant_transaction(first, st.a.agency):
            result["first"] = next_lead_number(first)
            thread = threading.Thread(target=allocate)
            thread.start()
            wait_until_lock_wait(admin, second.info.backend_pid)
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["second"] == result["first"] + 1


OPEN_ROW_INSERTS = {
    "lead": ("INSERT INTO lead (agency_id, conversation_id, lead_number) "
             "VALUES (%(agency)s, %(bare)s, 1000 + %(n)s)"),
    "handoff": ("INSERT INTO handoff (agency_id, conversation_id, lead_id) "
                "VALUES (%(agency)s, %(conversation)s, %(lead)s)"),
    "pending_clarification": ("INSERT INTO pending_clarification "
                              "(agency_id, conversation_id, slot, expires_at) "
                              "VALUES (%(agency)s, %(conversation)s, 'location', %(expires)s)"),
}


@pytest.mark.parametrize("table", sorted(OPEN_ROW_INSERTS))
@pytest.mark.parametrize("first_commits", [True, False])
def test_concurrent_opens_leave_one_open_row(st, admin, table, first_commits):
    """Two workers race to open a lead / handoff / clarification for one conversation.

    The second waits on the partial unique index; it fails if the first commits and
    succeeds if the first rolls back. Either way exactly one open row remains.
    """
    f = fresh(st.conn, st.a)

    def params(n):
        return {"agency": st.a.agency, "bare": f.bare, "conversation": f.conversation,
                "lead": f.lead, "expires": later(), "n": n}

    result = {}
    with psycopg.connect(st.app_url, autocommit=True) as first, psycopg.connect(
        st.app_url, autocommit=True
    ) as second:
        def race():
            try:
                with tenant_transaction(second, st.a.agency):
                    second.execute(OPEN_ROW_INSERTS[table], params(2))
                result["second"] = "inserted"
            except psycopg.errors.UniqueViolation:
                result["second"] = "unique_violation"

        expect_rollback = nullcontext() if first_commits else pytest.raises(RuntimeError)
        with expect_rollback, tenant_transaction(first, st.a.agency):
            first.execute(OPEN_ROW_INSERTS[table], params(1))
            thread = threading.Thread(target=race)
            thread.start()
            wait_until_lock_wait(admin, second.info.backend_pid)
            if not first_commits:
                raise RuntimeError("deliberate rollback")
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["second"] == ("unique_violation" if first_commits else "inserted")
    conversation = f.bare if table == "lead" else f.conversation
    status = "status IN ('pending', 'accepted')" if table == "handoff" else "status = 'open'"
    assert run(st, sql.SQL("SELECT count(*) FROM {} WHERE conversation_id = %s AND " + status)
               .format(sql.Identifier(table)), (conversation,)).fetchone() == (1,)


def test_one_open_handoff_per_conversation(st):
    a = st.a

    def request():
        run(st, "INSERT INTO handoff (agency_id, conversation_id, lead_id) VALUES (%s, %s, %s)",
            (a.agency, a.conversation, a.lead))

    with pytest.raises(psycopg.errors.UniqueViolation, match="handoff_one_open_idx"):
        request()
    run(st, "UPDATE handoff SET status = 'accepted', accepted_at = requested_at, "
            "accepted_by_staff_id = %s WHERE id = %s", (a.staff, a.handoff))
    with pytest.raises(psycopg.errors.UniqueViolation, match="handoff_one_open_idx"):
        request()
    run(st, "UPDATE handoff SET status = 'completed', closed_at = now() WHERE id = %s",
        (a.handoff,))
    request()


@pytest.mark.parametrize("assignment", [
    "status = 'accepted'",
    "accepted_at = requested_at",
    "accepted_at = requested_at, accepted_by_staff_id = assigned_staff_id",
    "status = 'cancelled'",
    "closed_at = now()",
    "status = 'mystery', closed_at = now()",
    "accept_due_at = requested_at",
    "release_due_at = requested_at - interval '1 hour'",
    (
        "status = 'accepted', accepted_by_staff_id = assigned_staff_id, "
        "accepted_at = requested_at - interval '1 minute'"
    ),
    "holding_reply_sent_at = requested_at - interval '1 minute'",
    "called_at = requested_at - interval '1 minute'",
    "outcome = 'deal'",
    "outcome_at = now()",
    "outcome = 'won', outcome_at = now()",
    "outcome = 'deal', outcome_at = requested_at - interval '1 minute'",
])
def test_handoff_checks(st, assignment):
    with pytest.raises(psycopg.errors.CheckViolation):
        run(st, f"UPDATE handoff SET {assignment} WHERE id = %s", (st.a.handoff,))


def test_handoff_lead_must_belong_to_the_same_conversation(st):
    f = fresh(st.conn, st.a)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        run(st, "INSERT INTO handoff (agency_id, conversation_id, lead_id) VALUES (%s, %s, %s)",
            (st.a.agency, f.conversation, st.a.lead))


def test_holding_reply_is_marked_at_most_once(st):
    statement = ("UPDATE handoff SET holding_reply_sent_at = now() "
                 "WHERE id = %s AND holding_reply_sent_at IS NULL")
    assert run(st, statement, (st.a.handoff,)).rowcount == 1
    assert run(st, statement, (st.a.handoff,)).rowcount == 0


def test_broker_buttons_record_called_and_outcome(st):
    run(st, "UPDATE handoff SET status = 'accepted', accepted_at = requested_at, "
            "accepted_by_staff_id = assigned_staff_id, called_at = now() WHERE id = %s",
        (st.a.handoff,))
    run(st, "UPDATE handoff SET outcome = 'visit_booked', outcome_at = now() WHERE id = %s",
        (st.a.handoff,))
    assert run(st, "SELECT outcome, called_at IS NOT NULL FROM handoff WHERE id = %s",
               (st.a.handoff,)).fetchone() == ("visit_booked", True)


def test_lead_can_cover_several_properties_once_each(st):
    other_property, _ = second_property(st)

    def add(prop):
        run(st, "INSERT INTO lead_property (agency_id, lead_id, property_id) VALUES (%s, %s, %s)",
            (st.a.agency, st.a.lead, prop))

    add(other_property)  # "Dono": both shown properties
    with pytest.raises(psycopg.errors.UniqueViolation):
        add(st.a.property)
    assert run(st, "SELECT count(*) FROM lead_property WHERE lead_id = %s",
               (st.a.lead,)).fetchone() == (2,)
