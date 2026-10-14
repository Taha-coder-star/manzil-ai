"""Task 8.2 slice 4b: find_listings and propose_update through the tool registry on a
disposable PostgreSQL (restricted login, RLS on). A proposal is an open update draft;
nothing here changes a property or confirms anything (9.2). All data is fictional."""

# ruff: noqa: F811  (tests take the imported inv fixture as a parameter)

import threading
import uuid

import psycopg
import pytest

from app import (
    ai_tools,
    demo_dataset,
    location,
    orchestrator,
    staff_drafts,
    staff_tools,
)
from app.db import tenant_transaction
from app.llm import ToolCall
from tests.test_inventory import active_property, insert, inv  # noqa: F401

GAZETTEER = location.Gazetteer.from_dataset(demo_dataset.load())


def staff_turn(conn, tenant, staff=None):
    staff = staff or tenant.staff
    with tenant_transaction(conn, tenant.agency):
        conversation = insert(conn, "conversation", agency_id=tenant.agency,
                              customer_wa_id="wa-" + uuid.uuid4().hex)
        job = insert(conn, "job", agency_id=tenant.agency, job_type="process_message",
                     dedupe_key="job-" + uuid.uuid4().hex)
        turn = insert(conn, "conversation_turn", agency_id=tenant.agency,
                      conversation_id=conversation, trigger_job_id=job, sender_role="agent",
                      route="staff", staff_id=staff, control_state="ai_active",
                      conversation_mode="ai", message_count=1)
    return orchestrator.Turn(
        id=turn, agency_id=tenant.agency, conversation_id=conversation, sender_wa_id="x",
        sender_role="agent", route="staff", staff_id=staff, control_state="ai_active",
        conversation_mode="ai", messages=())


def context(conn, turn, text):
    return ai_tools.ToolContext(
        "staff", text, staff=staff_drafts.context([text], (), False),
        binding=staff_tools.StaffBinding(conn, turn, GAZETTEER))


def call(name, args, ctx):
    return ai_tools.execute(ToolCall(name, args, None), ctx)


def propose(ctx, code, *changes):
    return call("propose_update", {"target": {"code": code}, "changes": [
        {"field": f, "span": s} for f, s in changes]}, ctx)


def add_property(conn, tenant, code, **overrides):
    with tenant_transaction(conn, tenant.agency):
        return insert(conn, "property", **active_property(
            tenant.agency, overrides.pop("location", None) or tenant.geo, listing_code=code,
            **overrides))


@pytest.fixture
def world(inv):
    a, b = inv.a, inv.b
    a.geo = b.geo = inv.geo.ph6
    inv.p1 = add_property(inv.conn, a, "DH6-901")  # sale house, 8 crore, 4 bed, active
    inv.p2 = add_property(inv.conn, a, "NNZ-901", purpose="rent", property_type="portion",
                          price_pkr=85_000, bedrooms=3)
    inv.p3 = add_property(inv.conn, b, "DH6-777")
    return inv


def drafts(conn, tenant):
    with tenant_transaction(conn, tenant.agency):
        return conn.execute(
            "SELECT id, status, version, kind, target_property_id, base_property_version, "
            "closed_at, staff_id, payload, ordinal FROM listing_draft "
            "WHERE kind = 'update' AND source_turn_id IS NOT NULL "  # not the fixture's own draft
            "ORDER BY created_at, id").fetchall()


def property_row(conn, tenant, code):
    with tenant_transaction(conn, tenant.agency):
        return conn.execute("SELECT price_pkr, status, version FROM property "
                            "WHERE listing_code = %s", (code,)).fetchone()


# --- propose_update ---------------------------------------------------------------------

def test_an_explicit_code_creates_one_open_update_draft_and_changes_nothing(world):
    w = world
    turn = staff_turn(w.conn, w.a)
    result = propose(context(w.conn, turn, "#DH6-901 price 7.8 crore"), "DH6-901",
                     ("price", "7.8 crore"))
    assert result.ok, result.error
    [row] = drafts(w.conn, w.a)
    assert (row[1], row[2], row[3], row[4], row[5]) == ("open", 1, "update", w.p1, 1)
    assert (row[7], row[9]) == (w.a.staff, 1)
    assert row[8]["target_code"] == "DH6-901"
    assert row[8]["changes"] == {"price_pkr": {"from": 80_000_000, "to": 78_000_000,
                                               "raw": "7.8 crore"}}
    assert property_row(w.conn, w.a, "DH6-901") == (80_000_000, "active", 1)  # untouched


def test_a_status_change_is_checked_against_the_listing(world):
    w = world.conn
    ok = propose(context(w, staff_turn(w, world.a), "DH6-901 sold ho gaya"), "DH6-901",
                 ("status", "sold"))
    assert ok.ok and drafts(w, world.a)[0][8]["changes"] == {
        "status": {"from": "active", "to": "sold", "raw": "sold"}}
    bad = propose(context(w, staff_turn(w, world.a), "NNZ-901 sold"), "NNZ-901",
                  ("status", "sold"))
    assert (bad.ok, bad.error) == (False, "sold_needs_sale")  # a rent listing is not sold
    noop = propose(context(w, staff_turn(w, world.a), "DH6-901 price 8 crore"), "DH6-901",
                   ("price", "8 crore"))
    assert (noop.ok, noop.error) == (False, "no_change")
    assert len(drafts(w, world.a)) == 1


def test_an_unknown_code_and_another_agencys_code_are_not_found(world):
    w = world.conn
    for code in ("DH6-999", "DH6-777"):  # DH6-777 exists only in agency B
        result = propose(context(w, staff_turn(w, world.a), f"{code} sold"), code,
                         ("status", "sold"))
        assert (result.ok, result.error) == (False, "target_not_found")
    assert drafts(w, world.a) == [] and drafts(w, world.b) == []


def test_a_code_the_staff_did_not_write_needs_exactly_one_candidate_from_this_turn(world):
    w = world.conn
    turn = staff_turn(w, world.a)
    ctx = context(w, turn, "portion rent ho gaya")
    assert propose(ctx, "NNZ-901", ("status", "rent")).error == "target_not_in_text"
    found = call("find_listings", {"property_type": "portion"}, ctx)
    assert found.ok and found.response["status"] == "single"
    assert found.response["candidates"][0]["code"] == "NNZ-901"
    done = propose(ctx, "NNZ-901", ("status", "rent"))
    assert done.ok, done.error
    assert drafts(w, world.a)[0][4] == world.p2


def test_several_candidates_make_a_choice_and_no_draft(world):
    w = world.conn
    turn = staff_turn(w, world.a)
    ctx = context(w, turn, "ghar sold")
    found = call("find_listings", {"property_type": "ghar"}, ctx)
    assert found.response["status"] == "several" and len(found.response["candidates"]) >= 2
    chosen = "DH6-901"  # one of the several: the model may not pick it
    result = propose(ctx, chosen, ("status", "sold"))
    assert (result.ok, result.error) == (False, "ambiguous_target")
    assert drafts(w, world.a) == []


def test_only_one_proposal_per_turn(world):
    w = world.conn
    ctx = context(w, staff_turn(w, world.a), "DH6-901 price 7.8 crore NNZ-901 sold")
    assert propose(ctx, "DH6-901", ("price", "7.8 crore")).ok
    second = propose(ctx, "NNZ-901", ("status", "sold"))
    assert (second.ok, second.error) == (False, "one_per_turn")
    assert len(drafts(w, world.a)) == 1


def test_a_closed_listing_cannot_be_repriced_but_can_return_to_active(world):
    w = world.conn
    with tenant_transaction(w, world.a.agency):
        w.execute("UPDATE property SET status = 'sold' WHERE listing_code = 'DH6-901'")
    bad = propose(context(w, staff_turn(w, world.a), "DH6-901 price 7 crore"), "DH6-901",
                  ("price", "7 crore"))
    assert (bad.ok, bad.error) == (False, "not_live")
    back = propose(context(w, staff_turn(w, world.a), "DH6-901 available"), "DH6-901",
                   ("status", "available"))
    assert back.ok


def test_the_turn_must_belong_to_the_staff_member_and_the_agency(world):
    w = world.conn
    turn = staff_turn(w, world.a)
    other = orchestrator.Turn(**{**turn.__dict__, "staff_id": world.a.other_staff})
    with pytest.raises(ValueError, match="staff"):
        propose(context(w, other, "DH6-901 sold"), "DH6-901", ("status", "sold"))
    foreign = orchestrator.Turn(**{**turn.__dict__, "agency_id": world.b.agency})
    with pytest.raises(LookupError):
        propose(context(w, foreign, "DH6-901 sold"), "DH6-901", ("status", "sold"))
    assert drafts(w, world.a) == [] and drafts(w, world.b) == []


# --- supersede, retry, concurrency (R-14) --------------------------------------------------

def test_a_newer_proposal_supersedes_the_open_one_for_the_same_staff_and_listing(world):
    w = world.conn
    first = staff_turn(w, world.a)
    propose(context(w, first, "DH6-901 price 7.8 crore"), "DH6-901", ("price", "7.8 crore"))
    other_staff = staff_turn(w, world.a, world.a.other_staff)  # another member keeps theirs
    propose(context(w, other_staff, "DH6-901 price 7 crore"), "DH6-901", ("price", "7 crore"))
    second = staff_turn(w, world.a)
    propose(context(w, second, "DH6-901 price 7.7 crore"), "DH6-901", ("price", "7.7 crore"))
    rows = drafts(w, world.a)
    assert [(r[1], r[7] == world.a.staff) for r in rows] == [
        ("superseded", True), ("open", False), ("open", True)]
    old = rows[0]
    assert old[2] == 2 and old[6] is not None  # closed, version bumped: old buttons are stale
    assert property_row(w, world.a, "DH6-901") == (80_000_000, "active", 1)


def test_retrying_an_earlier_turn_returns_its_original_draft_even_if_superseded(world):
    w = world.conn
    first = staff_turn(w, world.a)
    one = propose(context(w, first, "DH6-901 price 7.8 crore"), "DH6-901", ("price", "7.8 crore"))
    propose(context(w, staff_turn(w, world.a), "DH6-901 price 7.7 crore"), "DH6-901",
            ("price", "7.7 crore"))
    again = propose(context(w, first, "DH6-901 price 7.6 crore"), "DH6-901",
                    ("price", "7.6 crore"))  # the retry's model says something else
    assert again.ok and again.response["draft_id"] == one.response["draft_id"]
    assert (again.response["status"], again.response["draft_status"]) == (
        "existing", "superseded")
    rows = drafts(w, world.a)
    assert [r[1] for r in rows] == ["superseded", "open"] and len(rows) == 2
    assert rows[0][8]["changes"]["price_pkr"]["to"] == 78_000_000


def race(app_url, turns):
    """Each turn proposes a different price for DH6-901 on its own connection, together."""
    barrier, errors = threading.Barrier(len(turns)), []

    def run(turn, price):
        try:
            with psycopg.connect(app_url, autocommit=True) as conn:
                barrier.wait(timeout=10)
                result = propose(context(conn, turn, f"DH6-901 price {price}"), "DH6-901",
                                 ("price", price))
                assert result.ok, result.error
        except Exception as exc:  # noqa: BLE001  (reported by the test)
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(t, f"7.{n} crore"))
               for n, t in enumerate(turns, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return errors


def test_concurrent_proposals_for_one_listing_leave_one_open_draft(world, app_role_db):
    for _ in range(3):
        errors = race(app_role_db[1], [staff_turn(world.conn, world.a),
                                       staff_turn(world.conn, world.a)])
        assert not errors, errors
        mine = [r for r in drafts(world.conn, world.a) if r[7] == world.a.staff]
        assert [r[1] for r in mine].count("open") == 1
        assert {r[1] for r in mine} <= {"open", "superseded"}
