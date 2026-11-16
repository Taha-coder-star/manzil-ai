-- Audit MA-009 routing half (WP5, TD-11): which agency owns a connected WhatsApp number.
-- Read before any tenant context exists (agency is under FORCE RLS), so this table has
-- no RLS and no tenant column; the runtime may only read it. The operator writes it
-- through the schema-owner login (app.provision_agency). Empty = the runtime falls back
-- to WEBHOOK_SENDER_AGENCIES. app.routing.verify_agency still cross-checks every route
-- against the agency row under RLS.
CREATE TABLE sender_route (
    meta_phone_number_id TEXT PRIMARY KEY CHECK (meta_phone_number_id ~ '^[0-9]{5,32}$'),
    agency_id UUID NOT NULL REFERENCES agency(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
REVOKE ALL ON sender_route FROM PUBLIC, manzil_app;
GRANT SELECT ON sender_route TO manzil_app;
