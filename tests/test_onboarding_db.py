"""Audit WP5 (MA-008, MA-016, routing half of MA-009): routes from the database, operator
commands for agencies, staff and areas, the agency's name in customer text, '#demo' only
for the demo agency. Real worker, orchestrator and outbound path on the disposable
PostgreSQL (restricted manzil_app login, RLS on); WhatsApp is the FakeClient. All data
and numbers are fictional."""

# ruff: noqa: F811  (tests take the imported st fixture as a parameter)

import logging
import shutil
import threading
import uuid

import psycopg
import pytest

from app import (
    handlers,
    load_gazetteer,
    manage_staff,
    orchestrator,
    provision_agency,
    webhook,
)
from app.db import tenant_transaction
from app.ingest import ingest_events
from app.routing import DatabaseSenderResolver, EnvSenderResolver, RoutingConfigError
from app.seed import dd
from app.worker import Worker
from tests.test_customer_flow_db import turn
from tests.test_handoff_alert_db import ADMIN_NUMBER
from tests.test_handoff_db import BROKER_NUMBER, control
from tests.test_handoff_gate_db import to
from tests.test_requirements_db import (  # noqa: F401  (st: the 7.3 worker fixture)
    PNID,
    st,
)
from tests.test_search_db import CUSTOMER_A, CUSTOMER_B
from tests.test_turn_engine import text

PNID_B = "100000000000002"
GAZETTEER = ("locations.csv", "location_aliases.csv", "location_neighbors.csv")


def _envelope(pnid, *messages):
    return {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": pnid}, "messages": list(messages)}}]}]}


def _ingest(st, resolver, pnid, *messages):
    events, _ = webhook.extract_events(_envelope(pnid, *messages))
    return ingest_events(st.app_url, resolver, events)


def _env(st):
    return {"DATABASE_URL": st.app_url, "SEED_OWNER_DATABASE_URL": st.admin_url}


def _provision(st, capsys, *args):
    provision_agency.main([*args, "--apply"], _env(st))
    return capsys.readouterr().out


def _second_agency(st, capsys):
    out = _provision(st, capsys, "--name", "Second Realty", "--slug", "second",
                     "--phone-number-id", PNID_B)
    return uuid.UUID(out.split("Agency created: ")[1].split()[0])


def _routes(st):
    with psycopg.connect(st.admin_url, autocommit=True) as owner:
        return owner.execute("SELECT meta_phone_number_id, agency_id FROM sender_route "
                             "ORDER BY 1").fetchall()


# --- migration 021 ----------------------------------------------------------------------

def test_the_runtime_reads_routes_but_cannot_write_them(st):
    with psycopg.connect(st.app_url, autocommit=True) as app:
        assert app.execute("SELECT count(*) FROM sender_route").fetchone() == (0,)
        for statement in ("INSERT INTO sender_route VALUES ('1234567', %s)",
                          "DELETE FROM sender_route WHERE agency_id = %s"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(statement, (st.agency,))


# --- resolver ---------------------------------------------------------------------------

def test_routes_come_from_the_table_with_the_env_map_as_fallback(st, capsys):
    now = [0.0]
    resolver = DatabaseSenderResolver(st.app_url, f"{PNID}={st.agency}", clock=lambda: now[0])
    assert resolver.resolve(PNID) == st.agency                    # empty table: env map
    with pytest.raises(RoutingConfigError):
        DatabaseSenderResolver(st.app_url, "").resolve(PNID)     # neither: refused

    second = _second_agency(st, capsys)
    assert resolver.resolve(PNID_B) is None                       # cached for a minute
    now[0] = 61
    assert resolver.resolve(PNID_B) == second
    assert resolver.resolve(PNID) is None                         # the table wins
    assert resolver.agency_ids() == [second]


def test_a_route_to_the_wrong_agency_is_refused_by_the_cross_check(st, capsys):
    second = _second_agency(st, capsys)
    wrong = EnvSenderResolver({PNID: second})
    with pytest.raises(RoutingConfigError):
        _ingest(st, wrong, PNID, text("salam", sender=CUSTOMER_B))
    assert st.query("SELECT count(*) FROM inbound_event") == [(0,)]


def test_an_unknown_sender_is_dropped_with_an_alert(st, caplog):
    with caplog.at_level(logging.ERROR, logger="app.ingest"):
        result = _ingest(st, EnvSenderResolver({}), PNID_B, text("salam"))
    assert result.unknown_sender == 1
    assert "ALERT unknown_sender" in caplog.text and PNID_B not in caplog.text


# --- provision_agency -------------------------------------------------------------------

def test_provisioning_plans_by_default_and_routes_on_apply(st, capsys):
    provision_agency.main(["--name", "Second Realty", "--slug", "second",
                           "--phone-number-id", PNID_B], _env(st))
    assert "Plan only" in capsys.readouterr().out and _routes(st) == []

    second = _second_agency(st, capsys)
    out = _provision(st, capsys, "--agency-id", str(st.agency), "--phone-number-id", PNID)
    assert "Route added" in out and PNID not in out
    assert _routes(st) == [(PNID, st.agency), (PNID_B, second)]
    assert "Route exists" in _provision(st, capsys, "--agency-id", str(st.agency),
                                        "--phone-number-id", PNID)


def test_provisioning_refuses_a_number_the_agency_does_not_own(st, capsys):
    second = _second_agency(st, capsys)
    with pytest.raises(SystemExit, match="does not own"):
        _provision(st, capsys, "--agency-id", str(second), "--phone-number-id", PNID)
    assert _routes(st) == [(PNID_B, second)]


# --- acceptance: a second agency without a restart --------------------------------------

def test_two_agencies_are_each_answered_in_their_own_tenant_without_a_restart(st, capsys):
    resolver = DatabaseSenderResolver(st.app_url, f"{PNID}={st.agency}", ttl=0)
    worker = Worker(st.app_url, resolver.agency_ids(), st.handlers, threading.Event(),
                    backoff=lambda n: 0, reap_every_seconds=0, agencies=resolver.agency_ids)
    try:
        # Onboarding order (docs/ONBOARDING.md): route the live agency, then the new one.
        _provision(st, capsys, "--agency-id", str(st.agency), "--phone-number-id", PNID)
        second = _second_agency(st, capsys)
        staff_env = {"DATABASE_URL": st.app_url}
        manage_staff.main(["--agency-id", str(second), "add", "--name", "Sara",
                           "--phone", "+92 300 0000201", "--role", "admin"], staff_env)
        manage_staff.main(["--agency-id", str(second), "add", "--name", "Bilal",
                           "--phone", "923000000202", "--role", "broker"], staff_env)

        before = len(st.client.sent)
        _ingest(st, resolver, PNID, text("salam", sender=CUSTOMER_A))
        _ingest(st, resolver, PNID_B, text("salam", sender=CUSTOMER_B))
        while worker.run_once():
            pass
    finally:
        worker.close()

    assert {r for r, _ in st.client.sent[before:]} == {CUSTOMER_A, CUSTOMER_B}
    assert st.query("SELECT customer_wa_id FROM conversation") == [(CUSTOMER_A,)]
    assert st.query("SELECT customer_wa_id FROM conversation", agency_id=second) == [
        (CUSTOMER_B,)]
    with tenant_transaction(st.conn, second):
        assert orchestrator.resolve_role(st.conn, "923000000201")[:2] == ("admin", "staff")
        assert orchestrator.resolve_role(st.conn, "923000000202")[:2] == ("broker", "broker")
    out = capsys.readouterr().out
    assert "0000201" not in out and "...0201" in out


def test_demo_commands_run_only_for_the_demo_agency(st, capsys, monkeypatch):
    seen = {}
    monkeypatch.setattr(handlers.orchestrator, "run_turns",
                        lambda conn, job, *a, demo=None, **k: seen.__setitem__(
                            str(job.agency_id), demo is not None))
    _provision(st, capsys, "--agency-id", str(st.agency), "--phone-number-id", PNID)
    second = _second_agency(st, capsys)
    resolver = DatabaseSenderResolver(st.app_url, "", ttl=0)
    worker = Worker(st.app_url, resolver.agency_ids(), st.handlers, threading.Event(),
                    backoff=lambda n: 0)
    try:
        _ingest(st, resolver, PNID, text("#demo hi", sender=CUSTOMER_A))
        _ingest(st, resolver, PNID_B, text("#demo hi", sender=CUSTOMER_B))
        while worker.run_once():
            pass
    finally:
        worker.close()
    assert seen == {str(st.agency): True, str(second): False}


# --- manage_staff -----------------------------------------------------------------------

def test_deactivating_a_broker_passes_their_pending_lead_to_an_admin(st):
    turn(st, "Mujhe agent se baat karni hai", ("request_handoff", {"reason": "agent"}))
    assert control(st) == ("handoff_pending", "human")

    passed, kept = manage_staff.deactivate(st.conn, st.agency, BROKER_NUMBER)
    assert (passed, kept) == (["L-01"], [])
    assert st.query("SELECT s.role FROM handoff h JOIN staff s ON s.id = h.assigned_staff_id"
                    ) == [("admin",)]
    with tenant_transaction(st.conn, st.agency):
        assert orchestrator.resolve_role(st.conn, BROKER_NUMBER)[0] == "customer"

    before = len(st.client.sent)
    st.deliver(text("koi update?", sender=CUSTOMER_B))
    st.drain()
    # The forward goes to the admin (not sent here: the admin's 24 h window is closed).
    assert to(st, BROKER_NUMBER, before) == []
    assert st.query("SELECT DISTINCT recipient_wa_id FROM outbound_message "
                    "WHERE dedupe_key LIKE 'turn:%%:fwd:%%'") == [(ADMIN_NUMBER,)]
    with pytest.raises(manage_staff.StaffError, match="already inactive"):
        manage_staff.deactivate(st.conn, st.agency, BROKER_NUMBER)


def test_staff_commands_refuse_bad_input_and_change_roles(st, capsys):
    env = {"DATABASE_URL": st.app_url}
    with pytest.raises(SystemExit, match="already on the staff list"):
        manage_staff.main(["--agency-id", str(st.agency), "add", "--name", "Dup",
                           "--phone", BROKER_NUMBER, "--role", "agent"], env)
    with pytest.raises(SystemExit, match="international number"):
        manage_staff.main(["--agency-id", str(st.agency), "set-role", "--phone", "abc",
                           "--role", "agent"], env)
    manage_staff.main(["--agency-id", str(st.agency), "set-role", "--phone", BROKER_NUMBER,
                       "--role", "agent"], env)
    manage_staff.main(["--agency-id", str(st.agency), "list"], env)
    out = capsys.readouterr().out
    assert "agent   active   ...0103" in out and BROKER_NUMBER not in out


# --- load_gazetteer ---------------------------------------------------------------------

def _gazetteer_dir(tmp_path):
    folder = tmp_path / "areas"
    folder.mkdir()
    for name in GAZETTEER:
        shutil.copy(dd.DEMO_DIR / name, folder / name)
    return folder


def _counts(admin_url):
    with psycopg.connect(admin_url, autocommit=True) as owner:
        return [owner.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("location", "location_alias", "location_neighbor")]


def test_areas_load_from_a_folder_without_any_agency(app_role_db, tmp_path, capsys):
    admin_url = app_role_db[0]
    env = {"SEED_OWNER_DATABASE_URL": admin_url}
    folder = _gazetteer_dir(tmp_path)

    assert load_gazetteer.main(["--dir", str(folder)], env) == 0
    assert _counts(admin_url) == [0, 0, 0]                      # dry run writes nothing
    assert load_gazetteer.main(["--dir", str(folder), "--apply"], env) == 0
    assert _counts(admin_url) == [53, 87, 3]
    assert load_gazetteer.main(["--dir", str(folder)], env) == 0
    assert "location +0 =53" in capsys.readouterr().out

    # A conflicting edit of an existing area: reported, nothing written.
    locations = folder / "locations.csv"
    locations.write_text(locations.read_text("utf-8").replace(
        "Phase 6,DHA Phase 6,DH6,phase", "Phase 6,DHA Phase 6,DH6,sector", 1), "utf-8")
    assert load_gazetteer.main(["--dir", str(folder), "--apply"], env) == 2
    assert _counts(admin_url) == [53, 87, 3]


def test_another_market_can_be_loaded(app_role_db, tmp_path):
    """Not the demo's Karachi set: another city with one area and the official aliases."""
    admin_url = app_role_db[0]
    folder = _gazetteer_dir(tmp_path)

    def write(name, *rows):
        head = (dd.DEMO_DIR / name).read_text("utf-8").splitlines()[0]
        (folder / name).write_text("".join(line + "\n" for line in (head, *rows)), "utf-8")

    write("locations.csv",
          "lhr,,city,Lahore,Lahore,,,LHE,false,owner list,display_name",
          "lhr-gul,lhr,area,Gulberg,Gulberg,,,,false,owner list,display_name")
    official = ",,official,1,owner list,alias (canonical name)"
    write("location_aliases.csv", "lhr,lahore" + official, "lhr-gul,gulberg" + official)
    write("location_neighbors.csv")
    env = {"SEED_OWNER_DATABASE_URL": admin_url}
    assert load_gazetteer.main(["--dir", str(folder), "--apply"], env) == 0
    assert _counts(admin_url) == [2, 2, 0]


def test_invalid_area_files_are_refused_before_connecting(tmp_path, capsys):
    folder = _gazetteer_dir(tmp_path)
    (folder / "location_aliases.csv").write_text("wrong,header\n", "utf-8")
    assert load_gazetteer.main(["--dir", str(folder)], {}) == 1
    assert "could not be read" in capsys.readouterr().out
