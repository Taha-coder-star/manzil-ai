"""Task 8.2 slice 4a: migration 016 and create-draft persistence (disposable PostgreSQL,
restricted manzil_app login, RLS on). All data is fictional.

A staff turn's drafts are stored once: (agency_id, source_turn_id, ordinal) is the retry
identity, a retry returns the stored rows whatever the model said the second time, and
nothing is written to `property` (Save, commit and code allocation are 9.2).
"""

# ruff: noqa: F811  (tests take the imported inv fixture as a parameter)

import hashlib
import threading
import uuid
from pathlib import Path

import psycopg
import pytest

from app import demo_dataset, location, orchestrator, staff_drafts, staff_tools
from app import staff_normalise as sn
from app.db import tenant_transaction
from tests.test_inventory import admin, insert, inv  # noqa: F401

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
GAZETTEER = location.Gazetteer.from_dataset(demo_dataset.load())
PASTE = ("Phase 6 ghar 500 gaz 4 bed 8 crore sale\n"
         "Clifton block 5 shop 350 sq ft 2.5 lakh rent\n"
         "Phase 7 ghar 500 gaz 4 bed 7.5 crore sale")
SPANS = [
    {"purpose": "sale", "property_type": "ghar", "location": "Phase 6", "size": "500 gaz",
     "price": "8 crore", "bedrooms": "4 bed"},
    {"purpose": "rent", "property_type": "shop", "location": "Clifton block 5",
     "size": "350 sq ft", "price": "2.5 lakh"},
    {"purpose": "sale", "property_type": "ghar", "location": "Phase 7", "size": "500 gaz",
     "price": "7.5 crore", "bedrooms": "4 bed"},
]

# sha256 of the applied migrations as committed (LF bytes): 016 must not change any.
APPLIED = {
    "001_schema_v1.sql": "16b7dd48dfe1a85fbeb53a719c2a3b8cb7eb81b20d6ff77e33ee8c07d3e2056f",
    "002_tenant_rls.sql": "4af4070f6e0fae6c4a5d3e0e69550449b164ca33eede23b06938341cf3adf1fe",
    "003_job_conversation_key.sql":
        "6b348614df971f8afd6e7aaba8ca3a8ff0165cadc5210b77c7bbc90977b5095c",
    "004_outbound_delivery_status.sql":
        "0cc8d994920c2db9eeeaa5876ca399b2697d6a98f003369d5c0d8d54d57620a3",
    "005_inventory.sql": "5e4c60d2a00875965b8f07910c9252f82310d70d2e30f7f67f2c62031b1c9cbb",
    "006_conversation_state.sql":
        "652610a6a64bc586e3f2221691eba148e214c00ce02ec1dfedc2d21af23dddf5",
    "007_inventory_d11.sql": "08b44e0ec17dbe110bfd8543fcc446c2c85305835a5fbaebc43912d092ec42f2",
    "008_inbound_media.sql": "5b7af4299c359113ba3078cf759367b4fb149c1bb5722ac8502317abd249635f",
    "009_outbound_media.sql": "a8a78e928713608a7becf7fb828eacf9e0a9c87fc87a474839b2196a0831f69d",
    "010_conversation_turn.sql":
        "9e30852270ce3e9169294acfc7f735f13ae95d93e74e9ba95ef5495b6be0d91b",
    "011_carousel_fallback.sql":
        "46feecf70e04be1e10407e4addc8d0ee463d25d3685b8fe82c618928136ae237",
    "012_inbound_audio.sql": "6e3b34249c1617a62330bb4bcae99ee72851ff66a05bc5833d44747d719b677b",
    "013_usage_event_llm_detail.sql":
        "5fc8934d95b1c9cb0834ca077173eac7422a2a0f67c9f3bc413494c384de9f75",
    "014_conversation_turn_failed.sql":
        "1d8eec40c933b29a3ecdb95399aa77001512f5e3509848eeff63435d48ac5e0f",
    "015_staff_segment.sql": "ad1146fcdf033ba118aebeeda21859e6a4e5b9c8b77061e88fd50c79a596f2e6",
}


def make_turn(conn, tenant):
    """A staff turn row of the tenant (conversation, job, conversation_turn) as a Turn."""
    with tenant_transaction(conn, tenant.agency):
        conversation = insert(conn, "conversation", agency_id=tenant.agency,
                              customer_wa_id="wa-" + uuid.uuid4().hex)
        job = insert(conn, "job", agency_id=tenant.agency, job_type="process_message",
                     dedupe_key="job-" + uuid.uuid4().hex)
        turn = insert(conn, "conversation_turn", agency_id=tenant.agency,
                      conversation_id=conversation, trigger_job_id=job, sender_role="agent",
                      route="staff", staff_id=tenant.staff, control_state="ai_active",
                      conversation_mode="ai", message_count=1)
    return orchestrator.Turn(
        id=turn, agency_id=tenant.agency, conversation_id=conversation, sender_wa_id="x",
        sender_role="agent", route="staff", staff_id=tenant.staff,
        control_state="ai_active", conversation_mode="ai", messages=())


def drafts_of(count, spans=SPANS):
    texts = PASTE.split("\n")
    staff = staff_drafts.context([PASTE], {1}, True)
    result = sn.normalise_drafts(
        {"drafts": [{"text": t, **s} for t, s in zip(texts[:count], spans[:count],
                                                      strict=True)]},
        staff, GAZETTEER, marla_sqft=225, listings=[])
    return result["drafts"]


def rows_of(conn, tenant, turn):
    with tenant_transaction(conn, tenant.agency):
        return conn.execute(
            "SELECT id, ordinal, kind, status, version, staff_id, target_property_id, "
            "source_turn_id, payload FROM listing_draft WHERE source_turn_id = %s "
            "ORDER BY ordinal", (turn.id,)).fetchall()


def property_count(conn, tenant):
    with tenant_transaction(conn, tenant.agency):
        return conn.execute("SELECT count(*) FROM property").fetchone()[0]


# --- the migration ------------------------------------------------------------------------

def test_applied_migrations_are_byte_identical_and_016_follows_015():
    for name, digest in APPLIED.items():
        assert hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest() == digest, name
    names = sorted(p.name for p in MIGRATIONS.glob("0*.sql"))
    # 017 (Task 9.2, the staff card map) follows
    assert names[-3:] == ["015_staff_segment.sql", "016_listing_draft_turn_identity.sql",
                          "017_staff_card.sql"]
    assert all(b"\r" not in (MIGRATIONS / n).read_bytes() for n in names[-2:])


def test_the_migration_adds_exactly_the_planned_constraints(inv, admin):
    def constraints():
        return {r[0]: r[1] for r in admin.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'listing_draft'::regclass")}
    found = constraints()
    assert "superseded" in found["listing_draft_status_check"]
    assert "FOREIGN KEY (agency_id, source_turn_id) REFERENCES conversation_turn(agency_id, id)" \
        in found["listing_draft_source_turn_fkey"]
    assert {"listing_draft_ordinal_check", "listing_draft_turn_pair_check"} <= set(found)
    index = admin.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'listing_draft_turn_ordinal_key'"
    ).fetchone()[0]
    assert index.startswith("CREATE UNIQUE INDEX") and "(agency_id, source_turn_id, ordinal)" \
        in index and "WHERE (source_turn_id IS NOT NULL)" in index
    columns = {r[0]: (r[1], r[2]) for r in admin.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'listing_draft' AND table_schema = current_schema()")}
    assert columns["source_turn_id"] == ("uuid", "YES") and columns["ordinal"] == (
        "smallint", "YES")
    assert not {"conflicts", "staff_segment_id", "card_dedupe_key"} & set(columns)
    # the 005 rules are still there, and RLS and the grants are untouched
    assert {"listing_draft_one_open_update_idx", "listing_draft_open_staff_idx"} <= {
        r[0] for r in admin.execute("SELECT indexname FROM pg_indexes "
                                    "WHERE tablename = 'listing_draft'")}
    assert admin.execute("SELECT relforcerowsecurity FROM pg_class "
                         "WHERE oid = 'listing_draft'::regclass").fetchone() == (True,)


def test_raw_rows_obey_the_retry_identity_and_status_rules(inv):
    a, b = inv.a, inv.b
    turn, other = make_turn(inv.conn, a), make_turn(inv.conn, b)

    def draft(**extra):
        return {"agency_id": a.agency, "staff_id": a.staff, "kind": "create"} | extra

    with tenant_transaction(inv.conn, a.agency):
        insert(inv.conn, "listing_draft", **draft(source_turn_id=turn.id, ordinal=1))
        insert(inv.conn, "listing_draft", **draft(source_turn_id=turn.id, ordinal=2))
        insert(inv.conn, "listing_draft", **draft())  # drafts without a turn stay allowed
        insert(inv.conn, "listing_draft", **draft())

    def refuses(error, **extra):
        with pytest.raises(error), tenant_transaction(inv.conn, a.agency):
            insert(inv.conn, "listing_draft", **draft(**extra))

    refuses(psycopg.errors.UniqueViolation, source_turn_id=turn.id, ordinal=1)
    refuses(psycopg.errors.CheckViolation, source_turn_id=turn.id, ordinal=0)
    refuses(psycopg.errors.CheckViolation, source_turn_id=turn.id, ordinal=6)
    refuses(psycopg.errors.CheckViolation, source_turn_id=turn.id)  # no ordinal
    refuses(psycopg.errors.CheckViolation, ordinal=1)  # no turn
    refuses(psycopg.errors.CheckViolation, status="bogus", closed_at="2026-10-14T00:00:00Z")
    refuses(psycopg.errors.CheckViolation, status="superseded")  # a closed draft needs closed_at
    with tenant_transaction(inv.conn, a.agency):  # superseded is a closed status
        insert(inv.conn, "listing_draft", **draft(
            status="superseded", closed_at="2026-10-14T00:00:00Z"))
    # tenant isolation: another agency's turn cannot be referenced, nor read back
    refuses(psycopg.errors.ForeignKeyViolation, source_turn_id=other.id, ordinal=1)
    with tenant_transaction(inv.conn, b.agency):
        assert inv.conn.execute("SELECT count(*) FROM listing_draft WHERE source_turn_id = %s",
                                (turn.id,)).fetchone() == (0,)


# --- persistence --------------------------------------------------------------------------

def test_one_to_five_drafts_are_stored_in_one_transaction(inv):
    turn = make_turn(inv.conn, inv.a)
    before = property_count(inv.conn, inv.a)
    stored, created = staff_tools.save_drafts(inv.conn, turn, drafts_of(3))
    assert created is True and [s.ordinal for s in stored] == [1, 2, 3]
    rows = rows_of(inv.conn, inv.a, turn)
    assert [r[1] for r in rows] == [1, 2, 3]
    assert {(r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows} == {
        ("create", "open", 1, inv.a.staff, None, turn.id)}
    assert [r[0] for r in rows] == [s.id for s in stored]
    assert property_count(inv.conn, inv.a) == before  # nothing is written to property


def test_the_stored_payload_rebuilds_the_card_inputs_and_the_blockers(inv):
    turn = make_turn(inv.conn, inv.a)
    drafts = drafts_of(2)
    staff_tools.save_drafts(inv.conn, turn, drafts)
    for draft, row in zip(drafts, rows_of(inv.conn, inv.a, turn), strict=True):
        payload = row[8]
        assert payload["schema"] == 1 and payload["source_message"] == 1
        assert payload["fields"]["property_type"]["value"] == draft.fields[
            "property_type"]["value"]
        assert payload["proposed_code"] == draft.proposed_code
        assert payload["missing"] == list(draft.missing) and payload["conflicts"] == []
        assert payload["blockers"] == list(draft.blockers())
        # the blockers can be recomputed from the stored rows alone
        assert list(sn.save_blockers(payload["fields"], payload["conflicts"])) == \
            payload["blockers"]
        assert "buttons" not in payload and "actions" not in payload  # no Save is exposed


def test_a_draft_with_a_conflict_stores_its_conflict_and_blocker(inv):
    turn = make_turn(inv.conn, inv.a)
    text = "Phase 6 ghar 500 gaz 4 bed 5 bedrooms 8 crore sale"
    result = sn.normalise_drafts(
        {"drafts": [{"purpose": "sale", "property_type": "ghar", "location": "Phase 6",
                     "size": "500 gaz", "price": "8 crore", "bedrooms": "4 bed"}]},
        staff_drafts.context([text], {1}, True), GAZETTEER, marla_sqft=225, listings=[])
    staff_tools.save_drafts(inv.conn, turn, result["drafts"])
    payload = rows_of(inv.conn, inv.a, turn)[0][8]
    assert [c["field"] for c in payload["conflicts"]] == ["bedrooms"]
    assert payload["blockers"] == ["conflict:bedrooms"]


def test_a_retry_returns_the_stored_drafts_even_if_the_spans_differ(inv):
    turn = make_turn(inv.conn, inv.a)
    first, created = staff_tools.save_drafts(inv.conn, turn, drafts_of(2))
    changed = [dict(s, price="9 crore") for s in SPANS]  # the model would now say otherwise
    retry, again = staff_tools.save_drafts(inv.conn, turn, drafts_of(3, changed)[:3])
    assert created is True and again is False
    assert [r.id for r in retry] == [r.id for r in first]  # two drafts, not three
    assert [r.payload for r in retry] == [r.payload for r in first]
    assert len(rows_of(inv.conn, inv.a, turn)) == 2


def race(app_url, turn, counts):
    """Each count's save_drafts on its own connection, started together."""
    barrier, out, errors = threading.Barrier(len(counts)), [], []

    def run(count):
        try:
            with psycopg.connect(app_url, autocommit=True) as conn:
                barrier.wait(timeout=10)
                out.append(staff_tools.save_drafts(conn, turn, drafts_of(count)))
        except Exception as exc:  # noqa: BLE001  (reported by the test)
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(n,)) for n in counts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return out, errors


def test_concurrent_retries_of_one_turn_store_one_set(inv, app_role_db):
    for _ in range(4):
        turn = make_turn(inv.conn, inv.a)
        out, errors = race(app_role_db[1], turn, (2, 3))
        assert not errors, errors
        (one, made_one), (two, made_two) = out
        assert sorted([made_one, made_two]) == [False, True]  # exactly one writer
        assert [d.id for d in one] == [d.id for d in two]
        assert len(rows_of(inv.conn, inv.a, turn)) == len(one)
        assert len(one) in (2, 3)


# --- what save_drafts refuses -------------------------------------------------------------

def test_a_turn_of_another_agency_is_not_found_and_writes_nothing(inv):
    foreign = make_turn(inv.conn, inv.b)
    mine = orchestrator.Turn(**{**foreign.__dict__, "agency_id": inv.a.agency})
    with pytest.raises(LookupError):
        staff_tools.save_drafts(inv.conn, mine, drafts_of(1))
    assert rows_of(inv.conn, inv.b, foreign) == [] and rows_of(inv.conn, inv.a, mine) == []


def test_a_staff_member_cannot_draft_on_a_turn_that_is_not_theirs(inv):
    turn = make_turn(inv.conn, inv.a)
    other = orchestrator.Turn(**{**turn.__dict__, "staff_id": inv.a.other_staff})
    with pytest.raises(ValueError, match="staff"):
        staff_tools.save_drafts(inv.conn, other, drafts_of(1))
    assert rows_of(inv.conn, inv.a, turn) == []


@pytest.mark.parametrize("count", [0, 6])
def test_only_one_to_five_drafts_are_stored(inv, count):
    turn = make_turn(inv.conn, inv.a)
    drafts = drafts_of(1) * count
    with pytest.raises(ValueError, match="1-5"):
        staff_tools.save_drafts(inv.conn, turn, drafts)


def test_only_a_staff_turn_may_draft(inv):
    turn = make_turn(inv.conn, inv.a)
    customer = orchestrator.Turn(**{**turn.__dict__, "route": "customer", "staff_id": None})
    with pytest.raises(ValueError, match="staff"):
        staff_tools.save_drafts(inv.conn, customer, drafts_of(1))
