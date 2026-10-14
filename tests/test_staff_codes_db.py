"""Task 8.2 slice 3: the listing read behind duplicate warnings and code proposals is
scoped to the agency (disposable PostgreSQL, restricted manzil_app login, RLS on).

All data is fictional.
"""

# ruff: noqa: F811  (tests take the imported inv fixture as a parameter)

import uuid
from decimal import Decimal

from app import staff_tools
from app.db import tenant_transaction
from tests.test_inventory import active_property, insert, inv  # noqa: F401


def code_of(conn, agency, property_id):
    with tenant_transaction(conn, agency):
        return conn.execute("SELECT listing_code FROM property WHERE id = %s",
                            (property_id,)).fetchone()[0]


def test_listings_are_the_agencys_own_in_every_status(inv):
    a, b = inv.a, inv.b
    with tenant_transaction(inv.conn, a.agency):
        insert(inv.conn, "property", **active_property(
            a.agency, inv.geo.ph7, listing_code="DH7-205", status="sold", area_value=250,
            size_nominal_sqyd=250))
    rows_a = staff_tools.load_listings(inv.conn, a.agency)
    rows_b = staff_tools.load_listings(inv.conn, b.agency)

    own = code_of(inv.conn, a.agency, a.property)
    assert {r["code"] for r in rows_a} == {own, "DH7-205"}
    assert {r["code"] for r in rows_b} == {code_of(inv.conn, b.agency, b.property)}
    assert own not in {r["code"] for r in rows_b}

    sold = next(r for r in rows_a if r["code"] == "DH7-205")  # history is loaded too
    assert (sold["status"], sold["location_id"]) == ("sold", inv.geo.ph7)
    assert sold["sqft"] == Decimal(2250)  # the nominal sq-yd bucket x 9
    live = next(r for r in rows_a if r["code"] == own)
    assert (live["purpose"], live["property_type"], live["price_pkr"], live["bedrooms"]) == (
        "sale", "house", 80_000_000, 4)
    assert live["sqft"] == Decimal(4500)  # no nominal bucket: the actual size


def test_an_unknown_agency_gets_no_rows(inv):
    assert staff_tools.load_listings(inv.conn, uuid.uuid4()) == []
