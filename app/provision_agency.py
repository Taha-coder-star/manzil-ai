"""Create an agency bound to its connected WhatsApp sender, and its route (controlled admin step).

    python -m app.provision_agency --name "Acme Realty" --slug acme --phone-number-id <id>
    python -m app.provision_agency ... --apply                 # write (default: plan only)
    python -m app.provision_agency --agency-id <uuid> --phone-number-id <id> --apply
                                                               # route an existing agency

The agency row is written with the runtime DATABASE_URL (manzil_app). FORCE RLS blocks
even the table owner, so it is written inside a tenant transaction for a freshly
generated agency id, which satisfies the agency policy's WITH CHECK. The route
(sender_route, migration 021; audit WP5, TD-11) is shared routing data the runtime may
only read, so it is written with SEED_OWNER_DATABASE_URL (the schema owner), after the
agency is confirmed under RLS to own that phone number id. Not one transaction: if the
route step fails, rerun with --agency-id. The web and worker pick a new route up within a
minute, without a restart. docs/ONBOARDING.md has the full order.
"""

import argparse
import os
import sys
import uuid

import psycopg

from app.db import tenant_transaction
from app.routing import AgencyCheck, verify_agency

OWNER_URL_ENV = "SEED_OWNER_DATABASE_URL"


def create_agency(conn, name, slug, phone_number_id, waba_id=None, is_active=True):
    agency_id = uuid.uuid4()
    with tenant_transaction(conn, agency_id):
        conn.execute(
            "INSERT INTO agency (id, name, slug, meta_waba_id, meta_phone_number_id, is_active) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (agency_id, name, slug, waba_id, phone_number_id, is_active),
        )
    return agency_id


def owns(conn, agency_id, phone_number_id):
    """True when the agency exists under RLS and holds this phone number id."""
    with tenant_transaction(conn, agency_id):
        return verify_agency(conn, phone_number_id) is not AgencyCheck.MISMATCH


def add_route(owner, agency_id, phone_number_id):
    """Write the route; 'added', 'exists' (same agency) or 'taken' (another agency)."""
    with owner.transaction():
        row = owner.execute(
            "INSERT INTO sender_route (meta_phone_number_id, agency_id) VALUES (%s, %s) "
            "ON CONFLICT (meta_phone_number_id) DO NOTHING RETURNING agency_id",
            (phone_number_id, agency_id),
        ).fetchone()
        if row is not None:
            return "added"
        current = owner.execute(
            "SELECT agency_id FROM sender_route WHERE meta_phone_number_id = %s",
            (phone_number_id,),
        ).fetchone()[0]
    return "exists" if current == agency_id else "taken"


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name")
    parser.add_argument("--slug")
    parser.add_argument("--agency-id", help="route an existing agency instead of creating one")
    parser.add_argument("--phone-number-id", default=environ.get("META_PHONE_NUMBER_ID"))
    parser.add_argument("--waba-id", default=environ.get("META_WABA_ID"))
    parser.add_argument("--apply", action="store_true", help="write (default: plan only)")
    args = parser.parse_args(argv)

    database_url = environ.get("DATABASE_URL", "").strip()
    owner_url = environ.get(OWNER_URL_ENV, "").strip()
    if not database_url or not owner_url or not args.phone_number_id:
        sys.exit(f"DATABASE_URL, {OWNER_URL_ENV} and a phone number id "
                 "(or META_PHONE_NUMBER_ID) are required")
    if args.agency_id:
        try:
            agency_id = uuid.UUID(args.agency_id)
        except ValueError:
            sys.exit("--agency-id must be a UUID")
    elif not (args.name and args.slug):
        sys.exit("--name and --slug are required to create an agency")
    else:
        agency_id = None

    if not args.apply:
        what = (f"route ...{args.phone_number_id[-4:]} to agency {agency_id}" if agency_id
                else f"create agency {args.name!r} ({args.slug}) and route "
                     f"...{args.phone_number_id[-4:]} to it")
        print(f"Plan only: would {what}. Rerun with --apply to write.")
        return

    try:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
            if agency_id is None:
                agency_id = create_agency(
                    conn, args.name, args.slug, args.phone_number_id, args.waba_id)
                print(f"Agency created: {agency_id}")
            elif not owns(conn, agency_id, args.phone_number_id):
                sys.exit("That agency does not exist or does not own this phone number id; "
                         "nothing written")
        with psycopg.connect(owner_url, autocommit=True, connect_timeout=5) as owner:
            outcome = add_route(owner, agency_id, args.phone_number_id)
    except psycopg.errors.UniqueViolation:
        sys.exit("An agency with this slug or phone number id already exists")
    except psycopg.Error as exc:
        sys.exit(f"Agency provisioning failed: {type(exc).__name__}"
                 + (f"; agency {agency_id} exists, rerun with --agency-id" if agency_id else ""))
    if outcome == "taken":
        sys.exit("This phone number id is already routed to another agency; route unchanged")
    print(f"Route {outcome}: ...{args.phone_number_id[-4:]} -> {agency_id} "
          "(live within a minute, no restart)")


if __name__ == "__main__":
    main()
