"""Migration 007 (Task 3.3 D11 alignment): gazetteer levels, D11 listing facts, the
internal / public split, and the upgrade from schema 006.

All data is fictional. Tenant checks run as the restricted manzil_app login.
"""

import datetime
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app import migrate
from app.db import tenant_transaction
from tests import test_inventory as base
from tests.test_inventory import active_property, insert
from tests.test_rls_migration import become_migrator

INTERNAL_PROPERTY_COLUMNS = (
    "source_text", "internal_notes", "is_demo", "created_by_staff_id", "created_at",
    "source_type", "owner_name", "owner_phone", "floor_price_pkr", "commission_pct",
    "commission_note", "is_exclusive", "assigned_broker_id", "docs_status", "visit_notes",
    "address_detail", "source_message_ids", "duplicate_group_id",
)
PUBLIC_PROJECT_COLUMNS = [
    "id", "agency_id", "name", "location_id", "developer_name", "project_type",
    "construction_status", "expected_completion", "payment_plan_summary", "amenities",
    "updated_at",
]

# The 005 fixtures: shared Karachi gazetteer and two seeded tenants.
inv = base.inv
admin = base.admin


def zone(admin, parent, parent_level, name="Bukhari Commercial", **extra):
    return insert(admin, "location", level="zone", name=name, parent_id=parent,
                  parent_level=parent_level, **extra)


# Gazetteer -----------------------------------------------------------------------------------

def test_zone_under_sub_area_or_area(inv, admin):
    g = inv.geo
    bukhari = zone(admin, g.ph6, "sub_area", level_label="zone", is_commercial_zone=True)
    zone(admin, g.clifton, "area", name="Tariq Road (demo)")
    # Area-only search covers the whole subtree, zones included.
    assert admin.execute(
        "WITH RECURSIVE sub AS (SELECT id FROM location WHERE id = %s "
        "UNION ALL SELECT l.id FROM location l JOIN sub ON l.parent_id = sub.id) "
        "SELECT %s IN (SELECT id FROM sub)", (g.dha, bukhari),
    ).fetchone() == (True,)


@pytest.mark.parametrize("parent,parent_level,error", [
    ("karachi", "city", psycopg.errors.CheckViolation),
    ("ph6", "area", psycopg.errors.ForeignKeyViolation),
    (None, None, psycopg.errors.CheckViolation),
])
def test_zone_placement_is_strict(inv, admin, parent, parent_level, error):
    with pytest.raises(error):
        insert(admin, "location", level="zone", name="Invalid zone",
               parent_id=getattr(inv.geo, parent) if parent else None,
               parent_level=parent_level)


def test_nothing_hangs_below_a_zone(inv, admin):
    z = zone(admin, inv.geo.ph6, "sub_area")
    for level in ("zone", "sub_area"):
        with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation)):
            insert(admin, "location", level=level, name="Below zone", parent_id=z,
                   parent_level="zone")


def test_location_codes_and_attributes(inv, admin):
    g = inv.geo
    admin.execute("UPDATE location SET code = 'DH6', level_label = 'phase', "
                  "display_name = 'DHA Phase 6', centroid_lat = 24.79, centroid_lng = 67.06 "
                  "WHERE id = %s", (g.ph6,))
    admin.execute("UPDATE location SET city_code = 'KHI' WHERE id = %s", (g.karachi,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute("UPDATE location SET code = 'DH6' WHERE id = %s", (g.ph7,))


@pytest.mark.parametrize("column,value", [
    ("code", "dh6"), ("code", "TOOLONG"), ("level_label", "street"), ("city_code", "KHI"),
    ("centroid_lat", 24.8), ("display_name", " "),
])
def test_location_value_checks(inv, admin, column, value):
    # city_code is only valid on city rows; Phase 6 is a sub-area.
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(sql.SQL("UPDATE location SET {} = %s WHERE id = %s").format(
            sql.Identifier(column)), (value, inv.geo.ph6))


def test_alias_kind_and_weight(inv, admin):
    row = admin.execute(
        "SELECT kind, weight FROM location_alias WHERE location_id = %s", (inv.geo.ph6,)
    ).fetchone()
    assert row == ("common", 1)
    insert(admin, "location_alias", location_id=inv.geo.ph6, alias="ڈی ایچ اے 6",
           kind="urdu_script", weight=0.9)
    for overrides in ({"kind": "slang"}, {"weight": 1.5}, {"weight": -0.1}):
        with pytest.raises(psycopg.errors.CheckViolation):
            insert(admin, "location_alias", location_id=inv.geo.ph6,
                   alias="x" + uuid.uuid4().hex, **overrides)


def test_neighbor_weight(inv, admin):
    assert admin.execute("SELECT weight FROM location_neighbor").fetchall() == [(1,)]
    for weight in (0, 1.01):
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute("UPDATE location_neighbor SET weight = %s", (weight,))


# Listing facts -------------------------------------------------------------------------------

def add(inv, **overrides):
    with tenant_transaction(inv.conn, inv.a.agency):
        return insert(inv.conn, "property", **active_property(
            inv.a.agency, inv.geo.ph6, **overrides))


@pytest.mark.parametrize("overrides", [
    {"property_type": "penthouse"}, {"property_type": "showroom", "bedrooms": None},
    {"property_type": "warehouse", "bedrooms": None}, {"property_type": "building"},
    {"property_type": "floor", "bedrooms": None},
    {"status": "on_hold"}, {"status": "archived"},
    {"construction_status": "grey_structure"}, {"construction_status": "off_plan"},
    {"possession_status": "file", "expected_possession": datetime.date(2028, 12, 1)},
    {"installment_available": True, "investment_tags": ["installments", "pre_launch"]},
    {"is_tenanted": True, "investment_tags": ["rental_income"]},
    {"area_value": 272, "size_nominal_sqyd": 250, "covered_area_sqft": 3200},
    {"furnishing": "semi", "facing": ["west_open", "park_facing"], "is_corner": True,
     "features": ["basement", "drawing_dining"], "year_built": 2024},
    {"property_type": "portion", "portion_level": "upper"},
    {"public_features": Jsonb({"title": "500 yd West-open Bungalow", "parking_spaces": 2,
                               "rent_advance_months": 3})},
])
def test_inv_listing_facts_accepted(inv, overrides):
    add(inv, **overrides)


@pytest.mark.parametrize("overrides", [
    {"property_type": "villa"}, {"status": "draft"}, {"status": "ON_HOLD"},
    {"construction_status": "possession"}, {"possession_status": "ready"},
    {"expected_possession": datetime.date(2028, 12, 15)},
    {"investment_tags": ["yield"]}, {"investment_tags": ["capital_gain", "capital_gain"]},
    {"facing": ["west"]}, {"facing": [None]}, {"features": ["pool"]},
    {"furnishing": "partly"}, {"size_nominal_sqyd": 0}, {"covered_area_sqft": -1},
    {"year_built": 1800}, {"portion_level": "upper"}, {"portion_level": "top",
                                                       "property_type": "portion"},
    {"public_features": Jsonb({"owner_phone": "+92-300-0000001"})},
    {"public_features": Jsonb({"floor_price_pkr": 1})},
    {"public_features": Jsonb({"title": "x" * 61})},
    {"public_features": Jsonb({"title": " "})},
    {"public_features": Jsonb({"title": 5})},
    {"public_features": Jsonb({"description": "x" * 601})},
    {"commission_pct": 101}, {"floor_price_pkr": 0}, {"source_type": "broker"},
    {"docs_status": "clear"}, {"owner_name": ""}, {"source_message_ids": [None]},
])
def test_inv_listing_value_checks(inv, overrides):
    with pytest.raises(psycopg.errors.CheckViolation):
        add(inv, **overrides)


def test_penthouse_needs_bedrooms_to_publish(inv):
    with pytest.raises(psycopg.errors.CheckViolation, match="property_active_publishable"):
        add(inv, property_type="penthouse", bedrooms=None)


def test_last_confirmed_at_defaults_to_now_for_new_rows(inv):
    pid = add(inv)
    confirmed = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    explicit = add(inv, last_confirmed_at=confirmed)
    unconfirmed = add(inv, last_confirmed_at=None)
    with tenant_transaction(inv.conn, inv.a.agency):
        assert inv.conn.execute(
            "SELECT last_confirmed_at IS NOT NULL FROM property WHERE id = %s", (pid,)
        ).fetchone() == (True,)
        assert inv.conn.execute(
            "SELECT last_confirmed_at FROM property WHERE id = %s", (explicit,)
        ).fetchone() == (confirmed,)
        # NULL = never confirmed by staff; allowed, and still a searchable listing.
        assert inv.conn.execute(
            "SELECT last_confirmed_at FROM v_public_listing WHERE id = %s", (unconfirmed,)
        ).fetchone() == (None,)


def test_internal_fields_and_broker_assignment(inv):
    pid = add(inv, source_type="owner_direct", owner_name="Demo Owner",
              owner_phone="+92-300-0000101", floor_price_pkr=70_000_000, commission_pct=1,
              is_exclusive=True, assigned_broker_id=inv.a.other_staff, docs_status="unknown",
              address_detail="Street 12 (demo)", source_message_ids=["wamid.DEMO1"])
    with tenant_transaction(inv.conn, inv.a.agency):
        assert inv.conn.execute(
            "SELECT owner_phone, assigned_broker_id FROM property WHERE id = %s", (pid,)
        ).fetchone() == ("+92-300-0000101", inv.a.other_staff)


def test_cross_tenant_broker_rejected(inv):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        add(inv, assigned_broker_id=inv.b.staff)


# Events --------------------------------------------------------------------------------------

def demo_message(conn, t):
    conversation = insert(conn, "conversation", agency_id=t.agency,
                          customer_wa_id="demo-" + uuid.uuid4().hex)
    return insert(conn, "message", agency_id=t.agency, conversation_id=conversation,
                  direction="inbound", message_type="text")


def test_reconfirmed_event_with_source_message(inv):
    conn, t = inv.conn, inv.a
    with tenant_transaction(conn, t.agency):
        message = demo_message(conn, t)
        insert(conn, "property_event", agency_id=t.agency, property_id=t.property,
               event_type="reconfirmed", to_version=1, actor_staff_id=t.staff,
               source_message_id=message)


def test_cross_tenant_source_message_rejected(inv):
    conn = inv.conn
    with tenant_transaction(conn, inv.b.agency):
        message = demo_message(conn, inv.b)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        conn, inv.a.agency
    ):
        insert(conn, "property_event", agency_id=inv.a.agency, property_id=inv.a.property,
               event_type="reconfirmed", to_version=1, source_message_id=message)


# Projects, agency, staff ---------------------------------------------------------------------

def test_project_fields(inv):
    conn, t = inv.conn, inv.a
    with tenant_transaction(conn, t.agency):
        insert(conn, "project", agency_id=t.agency, name="Saahil View Residency (DEMO)",
               project_type="apartment_tower", construction_status="off_plan",
               expected_completion=datetime.date(2028, 12, 1),
               payment_plan_summary="30% down + 36 monthly", amenities=["lift", "gym"],
               approval_note="internal: NOC pending (demo)")
    for overrides in ({"project_type": "tower"}, {"construction_status": "file"},
                      {"expected_completion": datetime.date(2028, 12, 2)},
                      {"amenities": [None]}, {"payment_plan_summary": " "}):
        with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(conn, t.agency):
            insert(conn, "project", agency_id=t.agency, name="P " + uuid.uuid4().hex,
                   **overrides)


def test_agency_and_staff_settings(inv):
    conn, t = inv.conn, inv.a
    with tenant_transaction(conn, t.agency):
        assert conn.execute(
            "SELECT city_default, marla_sqft, settings FROM agency"
        ).fetchone() == ("KHI", 225, {})
        assert conn.execute(
            "SELECT areas_covered FROM staff WHERE id = %s", (t.staff,)
        ).fetchone() == ([],)
        conn.execute("UPDATE staff SET areas_covered = %s WHERE id = %s",
                     ([inv.geo.ph6], t.staff))
    for statement in ("UPDATE agency SET city_default = 'k'",
                      "UPDATE agency SET settings = '[]'",
                      "UPDATE staff SET areas_covered = ARRAY[NULL]::uuid[]"):
        with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(conn, t.agency):
            conn.execute(statement)


# Marla basis ---------------------------------------------------------------------------------

def set_marla(inv, t, value):
    with tenant_transaction(inv.conn, t.agency):
        inv.conn.execute("UPDATE agency SET marla_sqft = %s", (value,))


def area_sqft(inv, pid):
    with tenant_transaction(inv.conn, inv.a.agency):
        return inv.conn.execute(
            "SELECT area_sqft FROM property WHERE id = %s", (pid,)).fetchone()[0]


def test_agency_marla_default_and_valid_values(inv):
    with tenant_transaction(inv.conn, inv.a.agency):
        assert inv.conn.execute("SELECT marla_sqft FROM agency").fetchone() == (225,)
    set_marla(inv, inv.a, 272.25)
    set_marla(inv, inv.a, 225)


@pytest.mark.parametrize("value,error", [
    (0, psycopg.errors.CheckViolation), (-1, psycopg.errors.CheckViolation),
    (300, psycopg.errors.CheckViolation), (22.5, psycopg.errors.CheckViolation),
    (None, psycopg.errors.NotNullViolation),
])
def test_agency_marla_invalid_values_rejected(inv, value, error):
    with pytest.raises(error):
        set_marla(inv, inv.a, value)


@pytest.mark.parametrize("agency_marla,value,unit,sqft", [
    (225, 10, "marla", 2250), (225, 1, "kanal", 4500),
    (272.25, 10, "marla", 2722.5), (272.25, 1, "kanal", 5445),
])
def test_marla_and_kanal_use_the_agency_basis(inv, agency_marla, value, unit, sqft):
    set_marla(inv, inv.a, agency_marla)
    pid = add(inv, area_value=value, area_unit=unit, area_marla_sqft=agency_marla)
    assert area_sqft(inv, pid) == sqft


@pytest.mark.parametrize("value,unit,sqft", [(500, "sq_yd", 4500), (1200, "sq_ft", 1200)])
def test_non_marla_units_ignore_the_agency_basis(inv, value, unit, sqft):
    set_marla(inv, inv.a, 272.25)
    pid = add(inv, area_value=value, area_unit=unit)
    assert area_sqft(inv, pid) == sqft


@pytest.mark.parametrize("overrides", [
    {"area_value": 10, "area_unit": "marla"},
    {"area_value": 1, "area_unit": "kanal"},
    {"area_value": 500, "area_unit": "sq_yd", "area_marla_sqft": 225},
    {"area_value": None, "area_unit": None, "area_marla_sqft": 225, "status": "withdrawn"},
])
def test_marla_basis_required_exactly_for_marla_units(inv, overrides):
    with pytest.raises(psycopg.errors.CheckViolation, match="property_area_marla_basis_check"):
        add(inv, **overrides)


def test_marla_basis_must_match_the_agency_setting(inv):
    # The agency uses 225; a listing cannot claim 272.25.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        add(inv, area_value=10, area_unit="marla", area_marla_sqft=272.25)


def test_marla_basis_is_tenant_scoped(inv):
    # Agency B uses 272.25; agency A (225) cannot borrow it.
    set_marla(inv, inv.b, 272.25)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        add(inv, area_value=10, area_unit="marla", area_marla_sqft=272.25)


def test_agency_marla_cannot_change_under_marla_listings(inv):
    add(inv, area_value=10, area_unit="marla", area_marla_sqft=225)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        set_marla(inv, inv.a, 272.25)


# Customer-facing views -----------------------------------------------------------------------

def view_columns(admin, schema, view):
    return [r[0] for r in admin.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, view),
    ).fetchall()]


def test_internal_columns_never_in_views(inv, admin):
    listing = view_columns(admin, inv.schema, "v_public_listing")
    assert not set(INTERNAL_PROPERTY_COLUMNS) & set(listing)
    project = view_columns(admin, inv.schema, "v_public_project")
    assert project == PUBLIC_PROJECT_COLUMNS
    assert "approval_note" not in project and "is_demo" not in project


def test_on_hold_and_archived_hidden(inv):
    add(inv, status="on_hold")
    add(inv, status="archived")
    with tenant_transaction(inv.conn, inv.a.agency):
        assert {r[0] for r in inv.conn.execute(
            "SELECT status FROM v_public_listing").fetchall()} == {"active"}


def test_public_project_view_is_tenant_scoped_and_read_only(inv, admin):
    conn = inv.conn
    options = admin.execute(
        "SELECT reloptions FROM pg_class WHERE oid = 'v_public_project'::regclass"
    ).fetchone()[0]
    assert set(options) == {"security_invoker=true", "security_barrier=true"}
    assert conn.execute("SELECT count(*) FROM v_public_project").fetchone() == (0,)
    with tenant_transaction(conn, inv.a.agency):
        assert conn.execute("SELECT id FROM v_public_project").fetchall() == [(inv.a.project,)]
    granted = {p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
               if admin.execute("SELECT has_table_privilege('manzil_app', 'v_public_project', %s)",
                                (p,)).fetchone()[0]}
    assert granted == {"SELECT"}
    assert admin.execute(
        "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a "
        "WHERE c.oid = 'v_public_project'::regclass AND a.grantee = 0"
    ).fetchone() == (0,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_transaction(
        conn, inv.a.agency
    ):
        conn.execute("UPDATE v_public_project SET name = 'x'")


def test_vocabulary_helper_not_public(inv, admin):
    assert admin.execute(
        "SELECT has_function_privilege('public', 'text_set_within(text[], text[])', 'EXECUTE')"
    ).fetchone() == (False,)


# Upgrade from 006 ----------------------------------------------------------------------------

def schema_at_006_with_rows(conn, schema, **property_overrides):
    """Migrate to 006 as a non-superuser owner, then write inventory as manzil_app.

    Returns (007 SQL, owner role, agency id, area id, active property id); the
    connection is left as manzil_app with the agency context set.
    """
    conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA {}").format(
        sql.Identifier(schema)))
    become_migrator(conn, schema)
    migrations = migrate.load_migrations()
    at_007 = [name for name, _, _ in migrations].index("007_inventory_d11.sql")
    for _, _, statement in migrations[:at_007]:
        conn.execute(statement, prepare=False)

    karachi = insert(conn, "location", level="city", name="Karachi")
    dha = insert(conn, "location", level="area", name="DHA", parent_id=karachi,
                 parent_level="city")
    insert(conn, "location_alias", location_id=dha, alias="defence")

    conn.execute("RESET ROLE")
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO manzil_app").format(
        sql.Identifier(schema)))
    owner = conn.execute(
        "SELECT tableowner FROM pg_tables WHERE schemaname = %s "
        "AND tablename = 'property'", (schema,)
    ).fetchone()[0]
    agency = uuid.uuid4()
    conn.execute("SET LOCAL ROLE manzil_app")
    conn.execute("SELECT set_config('app.agency_id', %s, true)", (str(agency),))
    insert(conn, "agency", id=agency, name="Fictional (DEMO)", slug="demo")
    pid = insert(conn, "property", **active_property(
        agency, dha, public_features=Jsonb({}), **property_overrides))
    insert(conn, "property", agency_id=agency, listing_code="DH6-105", status="sold")
    return migrations[at_007][2], owner, agency, dha, pid


def test_upgrade_from_006_as_non_superuser_owner_preserves_rows(migration_database):
    """Existing inventory written under FORCE RLS survives 007 and gets its defaults."""
    database_url, schema = migration_database
    with psycopg.connect(database_url) as conn:
        try:
            statement, owner, agency, dha, pid = schema_at_006_with_rows(conn, schema)

            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner)))
            conn.execute(statement, prepare=False)

            conn.execute("SET LOCAL ROLE manzil_app")
            # Existing rows were never confirmed by staff: NULL, not the migration time.
            assert conn.execute(
                "SELECT count(*), bool_and(last_confirmed_at IS NULL) FROM property"
            ).fetchone() == (2, True)
            assert conn.execute(
                "SELECT area_sqft, area_marla_sqft FROM property WHERE id = %s", (pid,)
            ).fetchone() == (4500, None)
            new = insert(conn, "property", **active_property(agency, dha))
            assert conn.execute(
                "SELECT last_confirmed_at IS NOT NULL FROM property WHERE id = %s", (new,)
            ).fetchone() == (True,)
            assert sorted(conn.execute("SELECT id FROM v_public_listing").fetchall()) == \
                sorted([(pid,), (new,)])
            assert conn.execute(
                "SELECT city_default, marla_sqft FROM agency"
            ).fetchone() == ("KHI", 225)
            assert conn.execute(
                "SELECT kind, weight FROM location_alias"
            ).fetchone() == ("common", 1)
        finally:
            conn.rollback()


def test_upgrade_fails_closed_on_a_marla_listing_without_basis(migration_database):
    """A pre-007 marla size has no recorded basis: 007 must abort, not assume 225."""
    database_url, schema = migration_database
    with psycopg.connect(database_url) as conn:
        try:
            statement, owner, *_ = schema_at_006_with_rows(
                conn, schema, area_value=10, area_unit="marla")
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner)))
            with pytest.raises(psycopg.errors.CheckViolation,
                               match="property_area_marla_basis_check"):
                conn.execute(statement, prepare=False)
        finally:
            conn.rollback()
