"""Operator command for an agency's staff (audit WP5, MA-008 / MA-016, TD-13, BD-5 A).

    python -m app.manage_staff --agency-id <uuid> list
    python -m app.manage_staff --agency-id <uuid> add --name "Ali" --phone 923001234567 --role agent
    python -m app.manage_staff --agency-id <uuid> set-role --phone 923001234567 --role admin
    python -m app.manage_staff --agency-id <uuid> deactivate --phone 923001234567

Runs as the runtime login (DATABASE_URL, manzil_app) inside app.db.tenant_transaction, so
RLS keeps it inside the one agency. A change applies to the person's next message; no
restart. Roles: admin and agent manage listings (BD-13: any of them may change any
listing of the agency), broker receives leads only.

Deactivating passes the person's pending leads to the first active admin (as the staff
"pass" action does; the admin gets the lead cards with their next message) and lists the
leads they had already accepted, which stay with them until released or closed. Phone
numbers are printed only as their last four digits.
"""

import argparse
import os
import sys
import uuid

import psycopg

from app.db import tenant_transaction
from app.seed import normalize_phone

ROLES = ("admin", "agent", "broker")


class StaffError(Exception):
    """A request that cannot be carried out; nothing was written."""


def _masked(number):
    return f"...{number[-4:]}"


def _find(conn, phone):
    row = conn.execute(
        "SELECT id, name, role, is_active FROM staff "
        "WHERE regexp_replace(whatsapp_number, '[^0-9]', '', 'g') = %s", (phone,)).fetchone()
    if row is None:
        raise StaffError(f"no staff member with number {_masked(phone)}")
    return row


def list_staff(conn, agency_id):
    with tenant_transaction(conn, agency_id):
        return conn.execute(
            "SELECT name, role, is_active, whatsapp_number FROM staff "
            "ORDER BY is_active DESC, role, created_at").fetchall()


def add(conn, agency_id, name, phone, role):
    with tenant_transaction(conn, agency_id):
        try:
            return conn.execute(
                "INSERT INTO staff (agency_id, name, whatsapp_number, role) "
                "VALUES (current_agency_id(), %s, %s, %s) RETURNING id",
                (name, phone, role)).fetchone()[0]
        except psycopg.errors.UniqueViolation:
            raise StaffError(f"number {_masked(phone)} is already on the staff list") from None


def set_role(conn, agency_id, phone, role):
    with tenant_transaction(conn, agency_id):
        staff_id = _find(conn, phone)[0]
        conn.execute("UPDATE staff SET role = %s, updated_at = now() WHERE id = %s",
                     (role, staff_id))


def deactivate(conn, agency_id, phone):
    """Returns (passed lead codes, accepted lead codes still held)."""
    with tenant_transaction(conn, agency_id):
        staff_id, _, _, active = _find(conn, phone)
        if not active:
            raise StaffError(f"number {_masked(phone)} is already inactive")
        pending = conn.execute(
            "SELECT h.id, l.code FROM handoff h JOIN lead l ON l.id = h.lead_id "
            "WHERE h.status = 'pending' AND h.assigned_staff_id = %s ORDER BY h.requested_at",
            (staff_id,)).fetchall()
        admin = conn.execute(
            "SELECT id FROM staff WHERE is_active AND role = 'admin' AND id <> %s "
            "ORDER BY created_at, id LIMIT 1", (staff_id,)).fetchone()
        if pending and admin is None:
            raise StaffError("they hold pending leads and no other active admin can take them")
        for handoff_id, _ in pending:
            conn.execute(
                "UPDATE handoff SET assigned_staff_id = %s, escalated_at = COALESCE("
                "escalated_at, now()), updated_at = now() WHERE id = %s", (admin[0], handoff_id))
            conn.execute(
                "UPDATE lead SET assigned_staff_id = %s, updated_at = now() "
                "WHERE id = (SELECT lead_id FROM handoff WHERE id = %s)", (admin[0], handoff_id))
        accepted = conn.execute(
            "SELECT l.code FROM handoff h JOIN lead l ON l.id = h.lead_id "
            "WHERE h.status = 'accepted' AND h.accepted_by_staff_id = %s "
            "ORDER BY h.requested_at", (staff_id,)).fetchall()
        conn.execute("UPDATE staff SET is_active = false, updated_at = now() WHERE id = %s",
                     (staff_id,))
    return [code for _, code in pending], [code for (code,) in accepted]


def _phone(value):
    phone = normalize_phone(value)
    if phone is None:
        raise StaffError("--phone must be an international number (country code first)")
    return phone


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agency-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    added = commands.add_parser("add")
    added.add_argument("--name", required=True)
    added.add_argument("--phone", required=True)
    added.add_argument("--role", required=True, choices=ROLES)
    changed = commands.add_parser("set-role")
    changed.add_argument("--phone", required=True)
    changed.add_argument("--role", required=True, choices=ROLES)
    commands.add_parser("deactivate").add_argument("--phone", required=True)
    args = parser.parse_args(argv)

    database_url = environ.get("DATABASE_URL", "").strip()
    if not database_url:
        sys.exit("DATABASE_URL is required")
    try:
        agency_id = uuid.UUID(args.agency_id)
    except ValueError:
        sys.exit("--agency-id must be a UUID")
    try:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
            if args.command == "list":
                for name, role, active, number in list_staff(conn, agency_id):
                    print(f"{role:7} {'active' if active else 'inactive':8} "
                          f"{_masked(number)}  {name}")
            elif args.command == "add":
                if not args.name.strip():
                    raise StaffError("--name must not be empty")
                add(conn, agency_id, args.name.strip(), _phone(args.phone), args.role)
                print(f"Added {args.role} {_masked(_phone(args.phone))}; "
                      "recognised from their next message")
            elif args.command == "set-role":
                set_role(conn, agency_id, _phone(args.phone), args.role)
                print(f"{_masked(_phone(args.phone))} is now {args.role}")
            else:
                passed, kept = deactivate(conn, agency_id, _phone(args.phone))
                print(f"Deactivated {_masked(_phone(args.phone))}. Pending leads passed to an "
                      f"admin: {', '.join(passed) or 'none'}. Accepted leads still with them "
                      f"(close or tell the admin): {', '.join(kept) or 'none'}")
    except StaffError as exc:
        sys.exit(f"Nothing written: {exc}")
    except psycopg.Error as exc:
        sys.exit(f"Staff change failed: {type(exc).__name__}")


if __name__ == "__main__":
    main()
