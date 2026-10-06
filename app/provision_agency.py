"""Create an agency bound to its connected WhatsApp sender (controlled admin step).

Run with the runtime DATABASE_URL (manzil_app). FORCE RLS blocks even the
table owner, so the row is written inside a tenant transaction for a freshly
generated agency id, which satisfies the agency policy's WITH CHECK.
Prints the agency id and the routing entry for WEBHOOK_SENDER_AGENCIES.

    python -m app.provision_agency --name "Manzil Demo Realty (DEMO)" --slug manzil-demo
"""

import argparse
import os
import sys
import uuid

import psycopg

from app.db import tenant_transaction


def create_agency(conn, name, slug, phone_number_id, waba_id=None, is_active=True):
    agency_id = uuid.uuid4()
    with tenant_transaction(conn, agency_id):
        conn.execute(
            "INSERT INTO agency (id, name, slug, meta_waba_id, meta_phone_number_id, is_active) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (agency_id, name, slug, waba_id, phone_number_id, is_active),
        )
    return agency_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--phone-number-id", default=os.environ.get("META_PHONE_NUMBER_ID"))
    parser.add_argument("--waba-id", default=os.environ.get("META_WABA_ID"))
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url or not args.phone_number_id:
        sys.exit("DATABASE_URL and a phone number id (or META_PHONE_NUMBER_ID) are required")
    try:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
            agency_id = create_agency(
                conn, args.name, args.slug, args.phone_number_id, args.waba_id
            )
    except psycopg.errors.UniqueViolation:
        sys.exit("An agency with this slug or phone number id already exists")
    except psycopg.Error as exc:
        sys.exit(f"Agency provisioning failed: {type(exc).__name__}")

    print(f"Agency created: {agency_id}")
    print(f"Add to .env: WEBHOOK_SENDER_AGENCIES={args.phone_number_id}={agency_id}")


if __name__ == "__main__":
    main()
