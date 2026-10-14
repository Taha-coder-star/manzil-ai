"""Task 3.3 inventory schema: constraints, tenant isolation, privileges and the public view.

All data is fictional. Tenant checks run as the restricted manzil_app login.
"""

import uuid
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.db import tenant_transaction

SHARED_TABLES = ("location", "location_alias", "location_neighbor")
TENANT_TABLES = (
    "project", "property", "property_media", "property_event", "listing_draft", "staff_session",
)
PUBLIC_COLUMNS = [
    "id", "agency_id", "listing_code", "status", "purpose", "property_type", "category",
    "location_id", "project_id", "price_pkr", "area_value", "area_unit", "area_sqft",
    "bedrooms", "bathrooms", "construction_status", "latitude", "longitude",
    "public_features", "cover_media_id", "version", "updated_at",
    # Appended by 007 (D11 alignment).
    "size_nominal_sqyd", "covered_area_sqft", "portion_level", "furnishing", "facing",
    "is_corner", "features", "year_built", "possession_status", "expected_possession",
    "installment_available", "is_tenanted", "investment_tags", "last_confirmed_at",
]
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
EXPECTED_PRIVILEGES = {
    "location": {"SELECT"},
    "location_alias": {"SELECT"},
    "location_neighbor": {"SELECT"},
    "v_public_listing": {"SELECT"},
    "project": {"SELECT", "INSERT", "UPDATE"},
    "property": {"SELECT", "INSERT", "UPDATE"},
    "property_event": {"SELECT", "INSERT"},
    "property_media": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "listing_draft": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "staff_session": {"SELECT", "INSERT", "UPDATE", "DELETE"},
}


def insert(conn, table, **row):
    has_id = table != "location_neighbor"
    cur = conn.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({}){}").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, row)),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
            sql.SQL(" RETURNING id" if has_id else ""),
        ),
        tuple(row.values()),
    )
    return cur.fetchone()[0] if has_id else None


def active_property(agency_id, location_id, **overrides):
    row = {
        "agency_id": agency_id, "listing_code": f"DH6-{uuid.uuid4().int % 10**9}",
        "status": "active", "purpose": "sale", "property_type": "house",
        "location_id": location_id, "price_pkr": 80_000_000, "area_value": 500,
        "area_unit": "sq_yd", "bedrooms": 4, "bathrooms": 5, "is_demo": True,
    }
    row.update(overrides)
    return row


def seed_tenant(conn, location_id):
    """One fictional agency with a row in every inventory table."""
    t = SimpleNamespace(agency=uuid.uuid4())
    with tenant_transaction(conn, t.agency):
        insert(conn, "agency", id=t.agency, name="Fictional agency (DEMO)", slug=uuid.uuid4().hex)
        t.staff = insert(conn, "staff", agency_id=t.agency, name="Demo agent",
                         whatsapp_number="demo-" + uuid.uuid4().hex, role="agent")
        t.other_staff = insert(conn, "staff", agency_id=t.agency, name="Demo admin",
                               whatsapp_number="demo-" + uuid.uuid4().hex, role="admin")
        t.project = insert(conn, "project", agency_id=t.agency, name="Demo Towers",
                           location_id=location_id)
        t.property = insert(conn, "property", **active_property(
            t.agency, location_id, project_id=t.project, created_by_staff_id=t.staff,
            source_text="internal staff text", internal_notes="internal note",
        ))
        t.property_media = insert(conn, "property_media", agency_id=t.agency,
                                  property_id=t.property, original_key="demo/a.jpg", sort_order=0)
        conn.execute("UPDATE property SET cover_media_id = %s WHERE id = %s",
                      (t.property_media, t.property))
        t.listing_draft = insert(conn, "listing_draft", agency_id=t.agency, staff_id=t.staff,
                                 kind="update", target_property_id=t.property,
                                 base_property_version=1)
        t.property_event = insert(conn, "property_event", agency_id=t.agency,
                                  property_id=t.property, event_type="created", to_version=1,
                                  actor_staff_id=t.staff, listing_draft_id=t.listing_draft)
        t.staff_session = insert(conn, "staff_session", agency_id=t.agency, staff_id=t.staff,
                                 focus_property_id=t.property, state=Jsonb({}))
    return t


@pytest.fixture
def inv(app_role_db):
    admin_url, app_url, schema = app_role_db
    geo = SimpleNamespace()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        geo.karachi = insert(admin, "location", level="city", name="Karachi")
        geo.dha = insert(admin, "location", level="area", name="DHA",
                         parent_id=geo.karachi, parent_level="city")
        geo.clifton = insert(admin, "location", level="area", name="Clifton",
                             parent_id=geo.karachi, parent_level="city")
        geo.ph6 = insert(admin, "location", level="sub_area", name="Phase 6",
                         parent_id=geo.dha, parent_level="area")
        geo.ph7 = insert(admin, "location", level="sub_area", name="Phase 7",
                         parent_id=geo.dha, parent_level="area")
        geo.block8 = insert(admin, "location", level="sub_area", name="Block 8",
                            parent_id=geo.clifton, parent_level="area")
        insert(admin, "location_alias", location_id=geo.ph6, alias="Phase 6")
        a, b = sorted([geo.ph6, geo.ph7])
        insert(admin, "location_neighbor", location_a_id=a, location_b_id=b)
    with psycopg.connect(app_url, autocommit=True) as conn:
        a, b = seed_tenant(conn, geo.ph6), seed_tenant(conn, geo.ph6)
        yield SimpleNamespace(conn=conn, admin_url=admin_url, schema=schema, geo=geo, a=a, b=b)


@pytest.fixture
def admin(inv):
    with psycopg.connect(inv.admin_url, autocommit=True) as conn:
        yield conn


# Shared gazetteer ---------------------------------------------------------------------------

@pytest.mark.parametrize("case,error", [
    ("city_with_parent", psycopg.errors.CheckViolation),
    ("area_without_parent", psycopg.errors.CheckViolation),
    ("parent_level_without_parent", psycopg.errors.CheckViolation),
    ("unknown_level", psycopg.errors.CheckViolation),
    ("area_under_area", psycopg.errors.ForeignKeyViolation),
    ("sub_area_under_city", psycopg.errors.ForeignKeyViolation),
    ("missing_parent", psycopg.errors.ForeignKeyViolation),
])
def test_location_hierarchy_is_strict(inv, admin, case, error):
    g = inv.geo
    rows = {
        "city_with_parent": {"level": "city", "parent_id": g.karachi, "parent_level": "city"},
        "area_without_parent": {"level": "area"},
        "parent_level_without_parent": {"level": "city", "parent_level": "city"},
        "unknown_level": {"level": "block", "parent_id": g.dha, "parent_level": "area"},
        "area_under_area": {"level": "area", "parent_id": g.dha, "parent_level": "city"},
        "sub_area_under_city": {"level": "sub_area", "parent_id": g.karachi,
                                "parent_level": "area"},
        "missing_parent": {"level": "area", "parent_id": uuid.uuid4(), "parent_level": "city"},
    }
    with pytest.raises(error):
        insert(admin, "location", name="Invalid", **rows[case])


def test_location_level_cannot_change_under_children(inv, admin):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "UPDATE location SET level = 'city', parent_id = NULL, parent_level = NULL "
            "WHERE id = %s", (inv.geo.dha,),
        )


def test_location_names_unique_per_parent(inv, admin):
    g = inv.geo
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(admin, "location", level="sub_area", name="phase 6", parent_id=g.dha,
               parent_level="area")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(admin, "location", level="city", name="KARACHI")
    # The same name under another parent is a different place.
    insert(admin, "location", level="sub_area", name="Phase 6", parent_id=g.clifton,
           parent_level="area")


def test_aliases_allow_ambiguity_but_not_duplicates(inv, admin):
    g = inv.geo
    # "phase 8" names a sub-area in two different societies: both mappings are allowed.
    other = insert(admin, "location", level="area", name="Other Society",
                   parent_id=g.karachi, parent_level="city")
    first = insert(admin, "location", level="sub_area", name="Phase 8", parent_id=g.dha,
                   parent_level="area")
    second = insert(admin, "location", level="sub_area", name="Phase 8", parent_id=other,
                    parent_level="area")
    insert(admin, "location_alias", location_id=first, alias="Phase 8")
    insert(admin, "location_alias", location_id=second, alias="phase 8")
    insert(admin, "location_alias", location_id=first, alias="ph 8", context_parent_id=g.dha)
    assert admin.execute(
        "SELECT count(DISTINCT location_id) FROM location_alias WHERE alias_norm = 'phase 8'"
    ).fetchone() == (2,)
    assert admin.execute(
        "SELECT alias_norm FROM location_alias WHERE alias = 'ph 8'"
    ).fetchone() == ("ph 8",)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(admin, "location_alias", location_id=first, alias="  PHASE   8 ")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(admin, "location_alias", location_id=first, alias="Ph 8", context_parent_id=g.dha)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(admin, "location_alias", location_id=first, alias="self", context_parent_id=first)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert(admin, "location_alias", location_id=first, alias="x",
               context_parent_id=uuid.uuid4())


def test_alias_trigram_index_and_matching(inv, admin):
    definition = admin.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s "
        "AND indexname = 'location_alias_trgm_idx'", (inv.schema,),
    ).fetchone()[0]
    assert "gin" in definition and "gin_trgm_ops" in definition and "alias_norm" in definition
    # The runtime role can use trigram matching on the read-only gazetteer.
    assert inv.conn.execute(
        "SELECT location_id FROM location_alias WHERE similarity(alias_norm, 'fase 6') > 0.3"
    ).fetchall() == [(inv.geo.ph6,)]


def test_neighbors_are_symmetric_pairs_stored_once(inv, admin):
    g = inv.geo
    a, b = sorted([g.ph6, g.ph7])
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(admin, "location_neighbor", location_a_id=b, location_b_id=a)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(admin, "location_neighbor", location_a_id=a, location_b_id=a)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(admin, "location_neighbor", location_a_id=a, location_b_id=b)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        low, high = sorted([g.block8, uuid.uuid4()])
        insert(admin, "location_neighbor", location_a_id=low, location_b_id=high)


@pytest.mark.parametrize("table", SHARED_TABLES)
@pytest.mark.parametrize("statement", [
    "INSERT INTO {} DEFAULT VALUES", "UPDATE {} SET created_at = now()", "DELETE FROM {}",
    "TRUNCATE {}",
])
def test_gazetteer_is_read_only_for_runtime(inv, table, statement):
    conn = inv.conn
    assert conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                        ).fetchone()[0] > 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_transaction(
        conn, inv.a.agency
    ):
        conn.execute(sql.SQL(statement).format(sql.Identifier(table)))


# Privileges and policies ---------------------------------------------------------------------

@pytest.mark.parametrize("relation", sorted(EXPECTED_PRIVILEGES))
def test_exact_runtime_privileges_and_no_public_access(inv, admin, relation):
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


def test_tenant_tables_force_rls_with_single_tenant_policy(inv, admin):
    rows = admin.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s "
        "AND c.relname = ANY(%s)", (inv.schema, list(TENANT_TABLES + SHARED_TABLES)),
    ).fetchall()
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        **{t: (True, True) for t in TENANT_TABLES}, **{t: (False, False) for t in SHARED_TABLES},
    }
    policies = admin.execute(
        "SELECT tablename, policyname, cmd, roles::text[], qual, with_check FROM pg_policies "
        "WHERE schemaname = %s AND tablename = ANY(%s)",
        (inv.schema, list(TENANT_TABLES + SHARED_TABLES)),
    ).fetchall()
    assert sorted(p[0] for p in policies) == sorted(TENANT_TABLES)
    for _, name, cmd, roles, qual, with_check in policies:
        assert (name, cmd, roles) == ("tenant_isolation", "ALL", ["manzil_app"])
        for expression in (qual, with_check):
            assert "agency_id = current_agency_id()" in expression


# Tenant isolation ----------------------------------------------------------------------------

def new_row(table, t, geo):
    """A fresh valid row for tenant t (used to prove RLS rejects it in other contexts)."""
    return {
        "project": {"name": "Project " + uuid.uuid4().hex},
        "property": active_property(t.agency, geo.ph6),
        "property_media": {"property_id": t.property, "original_key": "demo/new.jpg",
                           "sort_order": 99},
        "property_event": {"property_id": t.property, "event_type": "updated",
                           "from_version": 1, "to_version": 2},
        "listing_draft": {"staff_id": t.staff, "kind": "create"},
        "staff_session": {"staff_id": t.other_staff},
    }[table] | {"agency_id": t.agency}


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_two_tenants_and_missing_context(inv, table):
    conn = inv.conn
    query = sql.SQL("SELECT id FROM {}").format(sql.Identifier(table))
    assert conn.execute(query).fetchall() == []
    for t in (inv.a, inv.b):
        with tenant_transaction(conn, t.agency):
            assert conn.execute(query).fetchall() == [(getattr(t, table),)]


@pytest.mark.parametrize("table", TENANT_TABLES)
@pytest.mark.parametrize("context", ["other", "missing"])
def test_insert_requires_matching_tenant(inv, table, context):
    conn = inv.conn
    row = new_row(table, inv.b, inv.geo)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        if context == "other":
            with tenant_transaction(conn, inv.a.agency):
                insert(conn, table, **row)
        else:
            insert(conn, table, **row)
    with tenant_transaction(conn, inv.b.agency):
        insert(conn, table, **row)


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_cannot_move_row_to_other_tenant(inv, table):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        inv.conn.execute(
            sql.SQL("UPDATE {} SET agency_id = %s WHERE id = %s").format(sql.Identifier(table)),
            (inv.b.agency, getattr(inv.a, table)),
        )


@pytest.mark.parametrize("table", sorted(
    t for t in TENANT_TABLES if "UPDATE" in EXPECTED_PRIVILEGES[t]
))
def test_invisible_rows_cannot_be_updated_or_deleted(inv, table):
    conn, name = inv.conn, sql.Identifier(table)
    with tenant_transaction(conn, inv.a.agency):
        assert conn.execute(sql.SQL("UPDATE {} SET id = id WHERE id = %s").format(name),
                            (getattr(inv.b, table),)).rowcount == 0
        if "DELETE" in EXPECTED_PRIVILEGES[table]:
            assert conn.execute(sql.SQL("DELETE FROM {} WHERE id = %s").format(name),
                                (getattr(inv.b, table),)).rowcount == 0
        assert conn.execute(sql.SQL("UPDATE {} SET id = id WHERE id = %s").format(name),
                            (getattr(inv.a, table),)).rowcount == 1


def test_matching_tenant_can_delete_where_granted(inv):
    conn = inv.conn
    with tenant_transaction(conn, inv.a.agency):
        assert conn.execute("DELETE FROM staff_session").rowcount == 1
        assert conn.execute("DELETE FROM property_media").rowcount == 1
        assert conn.execute("SELECT cover_media_id FROM property").fetchone() == (None,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_transaction(
        conn, inv.a.agency
    ):
        conn.execute("DELETE FROM property")


@pytest.mark.parametrize("table,column,parent", [
    ("property", "project_id", "project"),
    ("property", "created_by_staff_id", "staff"),
    ("property", "cover_media_id", "property_media"),
    ("property_media", "property_id", "property"),
    ("listing_draft", "staff_id", "staff"),
    ("listing_draft", "target_property_id", "property"),
    ("staff_session", "staff_id", "staff"),
    ("staff_session", "focus_property_id", "property"),
])
def test_cross_tenant_references_rejected_on_update(inv, table, column, parent):
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        inv.conn.execute(
            sql.SQL("UPDATE {} SET {} = %s WHERE id = %s").format(
                sql.Identifier(table), sql.Identifier(column)
            ), (getattr(inv.b, parent), getattr(inv.a, table)),
        )


@pytest.mark.parametrize("column,parent", [
    ("property_id", "property"),
    ("actor_staff_id", "staff"),
    ("listing_draft_id", "listing_draft"),
    ("undoes_event_id", "property_event"),
])
def test_cross_tenant_event_references_rejected(inv, column, parent):
    row = new_row("property_event", inv.a, inv.geo)
    if column == "undoes_event_id":
        row["event_type"] = "undone"
    row[column] = getattr(inv.b, parent)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        insert(inv.conn, "property_event", **row)


@pytest.mark.parametrize("table,column,parent", [
    ("property", "project_id", "project"),
    ("property", "created_by_staff_id", "staff"),
    ("property_media", "property_id", "property"),
    ("listing_draft", "staff_id", "staff"),
    ("staff_session", "staff_id", "staff"),
])
def test_cross_tenant_references_rejected_on_insert(inv, table, column, parent):
    row = new_row(table, inv.a, inv.geo) | {column: getattr(inv.b, parent)}
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        insert(inv.conn, table, **row)


def test_every_tenant_reference_is_scoped_by_agency(inv, admin):
    """Catalog guard: a single-column FK between tenant tables would allow cross-tenant links."""
    foreign_keys = admin.execute(
        "SELECT c.conname, c.conrelid::regclass::text, c.confrelid::regclass::text, "
        "(SELECT array_agg(a.attname ORDER BY k.n) FROM unnest(c.conkey) WITH ORDINALITY k(att, n) "
        " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.att), "
        "(SELECT array_agg(a.attname ORDER BY k.n) FROM unnest(c.confkey) WITH ORDINALITY k(att, n) "
        " JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.att) "
        "FROM pg_constraint c WHERE c.contype = 'f' AND c.conrelid::regclass::text = ANY(%s)",
        (list(TENANT_TABLES),),
    ).fetchall()
    tenant_parents = [fk for fk in foreign_keys if fk[2] not in SHARED_TABLES + ("agency",)]
    # 005: 12; 007: property.assigned_broker_id, property_event.source_message_id;
    # 008: property_media.source_inbound_media_id; 016: listing_draft.source_turn_id.
    assert len(tenant_parents) == 16
    for name, _, _, columns, referenced in tenant_parents:
        assert (columns[0], referenced[0]) == ("agency_id", "agency_id"), name


def test_unknown_agency_context_sees_nothing_and_cannot_write(inv):
    conn, unknown = inv.conn, uuid.uuid4()
    with tenant_transaction(conn, unknown):
        for table in TENANT_TABLES + ("v_public_listing",):
            assert conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone() == (0,)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(conn, unknown):
        insert(conn, "project", agency_id=unknown, name="Orphan")


def test_malformed_session_context_fails_closed(inv):
    conn = inv.conn
    with pytest.raises(psycopg.errors.InvalidTextRepresentation), conn.transaction():
        conn.execute("SELECT set_config('app.agency_id', 'not-a-uuid', true)")
        conn.execute("SELECT id FROM property")


# Property facts and publication --------------------------------------------------------------

def test_unknown_facts_stay_null_on_unpublished_inventory(inv):
    conn = inv.conn
    with tenant_transaction(conn, inv.a.agency):
        pid = insert(conn, "property", agency_id=inv.a.agency, listing_code="DH6-900",
                     status="withdrawn")
        row = conn.execute(
            "SELECT purpose, property_type, category, location_id, project_id, price_pkr, "
            "area_value, area_unit, area_sqft, bedrooms, bathrooms, construction_status, "
            "latitude, longitude, public_features, version, cover_media_id "
            "FROM property WHERE id = %s", (pid,),
        ).fetchone()
    assert row == (None,) * 14 + ({}, 1, None)


@pytest.mark.parametrize("missing", ["purpose", "property_type", "location_id", "price_pkr",
                                     "area", "bedrooms"])
def test_active_listing_requires_minimum_to_save(inv, missing):
    row = active_property(inv.a.agency, inv.geo.ph6)
    if missing == "area":
        row.update(area_value=None, area_unit=None)
    else:
        row[missing] = None
    with pytest.raises(psycopg.errors.CheckViolation, match="property_active_publishable"), \
            tenant_transaction(inv.conn, inv.a.agency):
        insert(inv.conn, "property", **row)


def test_bedrooms_not_required_for_plots_or_commercial(inv):
    with tenant_transaction(inv.conn, inv.a.agency):
        for property_type in ("plot", "office", "shop"):
            insert(inv.conn, "property", **active_property(
                inv.a.agency, inv.geo.ph6, property_type=property_type,
                bedrooms=None, bathrooms=None,
            ))


def test_incomplete_property_cannot_be_activated(inv):
    conn = inv.conn
    with tenant_transaction(conn, inv.a.agency):
        pid = insert(conn, "property", agency_id=inv.a.agency, listing_code="DH6-901",
                     status="withdrawn", purpose="sale")
    with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(conn, inv.a.agency):
        conn.execute("UPDATE property SET status = 'active' WHERE id = %s", (pid,))


@pytest.mark.parametrize("value,unit,sqft", [
    (500, "sq_yd", 4500), (1200, "sq_ft", 1200), (10, "marla", 2250), (1, "kanal", 4500),
])
def test_area_sqft_conversion(inv, value, unit, sqft):
    # Since 007, marla / kanal sizes carry the agency's marla basis (default 225).
    basis = 225 if unit in ("marla", "kanal") else None
    with tenant_transaction(inv.conn, inv.a.agency):
        pid = insert(inv.conn, "property", **active_property(
            inv.a.agency, inv.geo.ph6, area_value=value, area_unit=unit,
            area_marla_sqft=basis,
        ))
        assert inv.conn.execute(
            "SELECT area_sqft FROM property WHERE id = %s", (pid,)
        ).fetchone()[0] == sqft


@pytest.mark.parametrize("overrides", [
    {"version": 0}, {"version": -1}, {"area_unit": None}, {"latitude": 24.8},
    {"price_pkr": 0}, {"bedrooms": -1}, {"status": "ACTIVE"}, {"status": "draft"},
    {"purpose": "lease"}, {"listing_code": "dh6-101"}, {"listing_code": "DH6 101"},
    {"public_features": Jsonb([])},
])
def test_property_value_checks(inv, overrides):
    with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        insert(inv.conn, "property", **active_property(inv.a.agency, inv.geo.ph6, **overrides))


def test_version_must_stay_positive_on_update(inv):
    with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        inv.conn.execute("UPDATE property SET version = 0")


def test_listing_code_unique_per_agency(inv):
    conn = inv.conn
    with tenant_transaction(conn, inv.a.agency):
        code = conn.execute("SELECT listing_code FROM property").fetchone()[0]
    with pytest.raises(psycopg.errors.UniqueViolation), tenant_transaction(conn, inv.a.agency):
        insert(conn, "property", **active_property(inv.a.agency, inv.geo.ph6, listing_code=code))
    with tenant_transaction(conn, inv.b.agency):
        insert(conn, "property", **active_property(inv.b.agency, inv.geo.ph6, listing_code=code))


def test_project_name_unique_per_agency(inv):
    with pytest.raises(psycopg.errors.UniqueViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        insert(inv.conn, "project", agency_id=inv.a.agency, name="demo towers")


# Media and cover -----------------------------------------------------------------------------

def add_property_with_media(conn, t, geo):
    pid = insert(conn, "property", **active_property(t.agency, geo.ph6))
    media = [insert(conn, "property_media", agency_id=t.agency, property_id=pid,
                    original_key=f"demo/{n}.jpg", sort_order=n) for n in range(2)]
    return pid, media


def test_cover_must_be_media_of_the_same_property(inv):
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        _, media = add_property_with_media(inv.conn, inv.a, inv.geo)
        inv.conn.execute("UPDATE property SET cover_media_id = %s WHERE id = %s",
                         (media[0], inv.a.property))


def test_deleting_cover_media_clears_only_the_cover(inv):
    conn = inv.conn
    with tenant_transaction(conn, inv.a.agency):
        pid, media = add_property_with_media(conn, inv.a, inv.geo)
        conn.execute("UPDATE property SET cover_media_id = %s WHERE id = %s", (media[1], pid))
        conn.execute("DELETE FROM property_media WHERE id = %s", (media[1],))
        assert conn.execute(
            "SELECT agency_id, id, cover_media_id FROM property WHERE id = %s", (pid,)
        ).fetchone() == (inv.a.agency, pid, None)


def test_cover_media_cannot_move_to_another_property(inv):
    conn = inv.conn
    with tenant_transaction(conn, inv.a.agency):
        pid, media = add_property_with_media(conn, inv.a, inv.geo)
        # Media that is not a cover may be reassigned within the agency.
        conn.execute("UPDATE property_media SET property_id = %s, sort_order = 50 WHERE id = %s",
                     (inv.a.property, media[1]))
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        conn, inv.a.agency
    ):
        conn.execute("UPDATE property_media SET property_id = %s WHERE id = %s",
                     (pid, inv.a.property_media))


def test_media_order_and_hash_unique_per_property(inv):
    conn = inv.conn
    digest = "a" * 64
    with pytest.raises(psycopg.errors.UniqueViolation), tenant_transaction(conn, inv.a.agency):
        insert(conn, "property_media", agency_id=inv.a.agency, property_id=inv.a.property,
               original_key="demo/dup.jpg", sort_order=0)
    with pytest.raises(psycopg.errors.UniqueViolation), tenant_transaction(conn, inv.a.agency):
        for n in (5, 6):
            insert(conn, "property_media", agency_id=inv.a.agency, property_id=inv.a.property,
                   original_key=f"demo/{n}.jpg", sort_order=n, content_sha256=digest)
    with tenant_transaction(conn, inv.a.agency):
        pid, (first, second) = add_property_with_media(conn, inv.a, inv.geo)
        # Swap the two images; the order key is checked at commit.
        conn.execute("SET CONSTRAINTS property_media_order_key DEFERRED")
        conn.execute("UPDATE property_media SET sort_order = 1 WHERE id = %s", (first,))
        conn.execute("UPDATE property_media SET sort_order = 0 WHERE id = %s", (second,))
        assert conn.execute(
            "SELECT id FROM property_media WHERE property_id = %s ORDER BY sort_order", (pid,)
        ).fetchall() == [(second,), (first,)]


# Audit events -------------------------------------------------------------------------------

@pytest.mark.parametrize("statement", [
    "UPDATE property_event SET changes = '{}'", "DELETE FROM property_event",
    "TRUNCATE property_event",
])
def test_property_event_is_append_only(inv, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        inv.conn.execute(statement)


def test_undo_must_reference_event_of_same_property(inv):
    conn = inv.conn
    with tenant_transaction(conn, inv.a.agency):
        undo = insert(conn, "property_event", agency_id=inv.a.agency, property_id=inv.a.property,
                      event_type="undone", from_version=1, to_version=2,
                      undoes_event_id=inv.a.property_event, changes=Jsonb({}))
        assert undo
    with pytest.raises(psycopg.errors.ForeignKeyViolation), tenant_transaction(
        conn, inv.a.agency
    ):
        pid, _ = add_property_with_media(conn, inv.a, inv.geo)
        insert(conn, "property_event", agency_id=inv.a.agency, property_id=pid,
               event_type="undone", to_version=2, undoes_event_id=inv.a.property_event)


@pytest.mark.parametrize("overrides", [
    {"event_type": "undone"}, {"from_version": 2, "to_version": 2}, {"to_version": 0},
    {"event_type": "deleted"}, {"changes": Jsonb([])},
])
def test_property_event_checks(inv, overrides):
    row = new_row("property_event", inv.a, inv.geo) | overrides
    with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        insert(inv.conn, "property_event", **row)


# Drafts and sessions -------------------------------------------------------------------------

def update_draft(t, staff=None):
    return {"agency_id": t.agency, "staff_id": staff or t.staff, "kind": "update",
            "target_property_id": t.property, "base_property_version": 1}


def test_one_open_update_draft_per_staff_and_property(inv):
    conn, a = inv.conn, inv.a
    with pytest.raises(psycopg.errors.UniqueViolation), tenant_transaction(conn, a.agency):
        insert(conn, "listing_draft", **update_draft(a))
    with tenant_transaction(conn, a.agency):
        # Another staff member may draft the same property; saving settles it by version.
        insert(conn, "listing_draft", **update_draft(a, a.other_staff))
        conn.execute("UPDATE listing_draft SET status = 'cancelled', closed_at = now() "
                     "WHERE id = %s", (a.listing_draft,))
        insert(conn, "listing_draft", **update_draft(a))
        for _ in range(3):
            insert(conn, "listing_draft", agency_id=a.agency, staff_id=a.staff, kind="create")
        assert conn.execute(
            "SELECT count(*) FROM listing_draft WHERE status = 'open' AND kind = 'create'"
        ).fetchone() == (3,)


@pytest.mark.parametrize("overrides", [
    {"kind": "create"},
    {"target_property_id": None},
    {"base_property_version": None},
    {"base_property_version": 0},
    {"version": 0},
    {"status": "saved"},
    {"closed_at": "2026-10-08T00:00:00Z"},
    {"payload": Jsonb([])},
])
def test_listing_draft_checks(inv, overrides):
    with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        insert(inv.conn, "listing_draft", **update_draft(inv.a, inv.a.other_staff) | overrides)


def test_one_staff_session_per_staff(inv):
    with pytest.raises(psycopg.errors.UniqueViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        insert(inv.conn, "staff_session", agency_id=inv.a.agency, staff_id=inv.a.staff)
    with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        insert(inv.conn, "staff_session", agency_id=inv.a.agency, staff_id=inv.a.other_staff,
               state=Jsonb([]))


def test_staff_roles_unchanged(inv, admin):
    definition = admin.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'staff'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%role%'"
    ).fetchone()[0]
    assert all(role in definition for role in ("'admin'", "'agent'", "'broker'"))
    with pytest.raises(psycopg.errors.CheckViolation), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        insert(inv.conn, "staff", agency_id=inv.a.agency, name="x", whatsapp_number="x",
               role="staff")


# Public listing view -------------------------------------------------------------------------

def test_public_view_columns_are_the_allowlist(inv, admin):
    columns = [r[0] for r in admin.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'v_public_listing' ORDER BY ordinal_position",
        (inv.schema,),
    ).fetchall()]
    assert columns == PUBLIC_COLUMNS
    for internal in ("source_text", "internal_notes", "is_demo", "created_by_staff_id"):
        assert internal not in columns
    options = admin.execute(
        "SELECT reloptions FROM pg_class WHERE oid = 'v_public_listing'::regclass"
    ).fetchone()[0]
    assert set(options) == {"security_invoker=true", "security_barrier=true"}


def test_public_view_shows_only_active_rows_of_current_agency(inv):
    conn = inv.conn
    assert conn.execute("SELECT count(*) FROM v_public_listing").fetchone() == (0,)
    with tenant_transaction(conn, inv.a.agency):
        for status in ("reserved", "sold", "rented", "withdrawn"):
            insert(conn, "property", **active_property(inv.a.agency, inv.geo.ph6, status=status))
        assert {r[0] for r in conn.execute("SELECT status FROM property").fetchall()} == {
            "active", "reserved", "sold", "rented", "withdrawn",
        }
        assert conn.execute("SELECT id, status FROM v_public_listing").fetchall() == [
            (inv.a.property, "active"),
        ]
        conn.execute("UPDATE property SET status = 'sold' WHERE id = %s", (inv.a.property,))
        assert conn.execute("SELECT count(*) FROM v_public_listing").fetchone() == (0,)
    with tenant_transaction(conn, inv.b.agency):
        assert conn.execute("SELECT id, agency_id FROM v_public_listing").fetchall() == [
            (inv.b.property, inv.b.agency),
        ]


@pytest.mark.parametrize("statement", [
    "UPDATE v_public_listing SET price_pkr = 1",
    "DELETE FROM v_public_listing",
    (
        "INSERT INTO v_public_listing (agency_id, listing_code, status) "
        "VALUES (current_agency_id(), 'DH6-999', 'withdrawn')"
    ),
])
def test_public_view_is_read_only(inv, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_transaction(
        inv.conn, inv.a.agency
    ):
        inv.conn.execute(statement)


def test_public_view_does_not_bypass_base_table_privileges(inv):
    with psycopg.connect(inv.admin_url) as conn:
        try:
            role = sql.Identifier("test_view_only_" + uuid.uuid4().hex)
            conn.execute(sql.SQL("CREATE ROLE {}").format(role))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(inv.schema), role))
            conn.execute(sql.SQL("GRANT SELECT ON v_public_listing TO {}").format(role))
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(role))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM v_public_listing")
        finally:
            conn.rollback()
