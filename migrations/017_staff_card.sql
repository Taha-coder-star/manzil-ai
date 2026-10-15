-- Task 9.2: which draft or listing a staff card is about (docs/STAFF_COMMIT.md section 3;
-- Red-Team R-08 / R-10: reply-to-card attach and correction).
-- A staff member answers a card by swiping it: the inbound message then quotes the card's
-- WAMID (message.context_wamid). That WAMID is on outbound_message.provider_message_id, its
-- dedupe_key names the send, and this table maps the dedupe_key to the draft or listing the
-- card showed. The row is written before the card is sent (the key is known then), so a
-- crash between the two leaves a mapping without a send, never a send without a mapping.
-- No FK to outbound_message: a card held back by the send gate is never recorded there.
-- Additive: one new table. Run as the schema owner, never as the runtime login.
-- Requires 005, 016.

CREATE TABLE staff_card (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    -- The outbound dedupe key the card is sent under (turn:{turn_id}:{seq}).
    dedupe_key TEXT NOT NULL CHECK (btrim(dedupe_key) <> ''),
    listing_draft_id UUID,
    property_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, dedupe_key),

    -- Same agency as the card: another agency's draft or listing cannot be referenced.
    FOREIGN KEY (agency_id, listing_draft_id)
        REFERENCES listing_draft(agency_id, id),
    FOREIGN KEY (agency_id, property_id)
        REFERENCES property(agency_id, id),

    -- A card is about exactly one draft or one listing.
    CONSTRAINT staff_card_one_ref_check
        CHECK ((listing_draft_id IS NULL) <> (property_id IS NULL))
);

CREATE INDEX staff_card_draft_idx
    ON staff_card (agency_id, listing_draft_id);

CREATE INDEX staff_card_property_idx
    ON staff_card (agency_id, property_id);


-- Privileges and RLS follow the 002 / 005 conventions.
DO $migration$
DECLARE
    target_schema TEXT := current_schema();
    app_role RECORD;
BEGIN
    SELECT * INTO app_role
    FROM pg_catalog.pg_roles WHERE rolname = 'manzil_app';

    IF NOT FOUND OR app_role.rolsuper OR app_role.rolbypassrls THEN
        RAISE EXCEPTION 'manzil_app is missing or has unsafe attributes';
    END IF;

    EXECUTE format('REVOKE ALL ON TABLE %I.staff_card FROM PUBLIC, manzil_app',
                   target_schema);
    EXECUTE format('ALTER TABLE %I.staff_card ENABLE ROW LEVEL SECURITY', target_schema);
    EXECUTE format('ALTER TABLE %I.staff_card FORCE ROW LEVEL SECURITY', target_schema);
    EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I.staff_card FOR ALL TO manzil_app '
        'USING (agency_id = %I.current_agency_id()) '
        'WITH CHECK (agency_id = %I.current_agency_id())',
        target_schema, target_schema, target_schema
    );
    -- The map is history: never UPDATE or DELETE.
    EXECUTE format('GRANT SELECT, INSERT ON TABLE %I.staff_card TO manzil_app',
                   target_schema);
END
$migration$;
