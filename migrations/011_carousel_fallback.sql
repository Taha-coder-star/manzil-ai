-- Task 5.2: per-contact carousel fallback (D9, Red-Team R-39, plan risk 4) and the
-- fixed reply plan of a turn.
-- A conversation is one row per (agency, customer_wa_id) for the contact's whole
-- life (001 UNIQUE; ingestion only upserts; closed is a control_state of the same
-- row), so the preference survives a closed conversation and covers every device
-- of the contact. When set, results go out as individual image cards instead of
-- the interactive media carousel. Design: docs/STATE_SCHEMA.md.
-- Additive: nullable columns plus one constant default, so no row is rewritten or
-- changed. The runtime keeps its table-level conversation grants (002) and its
-- conversation_turn grants (010); FORCE RLS is untouched. Run as the schema owner,
-- never as the runtime login. Requires 006 and 010.


ALTER TABLE conversation
    ADD COLUMN carousel_fallback BOOLEAN NOT NULL DEFAULT false,
    -- Last change of the flag (either direction) and the staff member who made it.
    ADD COLUMN carousel_fallback_set_at TIMESTAMPTZ,
    ADD COLUMN carousel_fallback_set_by UUID,
    ADD CONSTRAINT conversation_carousel_fallback_staff_fkey
        FOREIGN KEY (agency_id, carousel_fallback_set_by)
        REFERENCES staff(agency_id, id),
    -- A set flag, or a recorded setter, always has a change time.
    ADD CONSTRAINT conversation_carousel_fallback_set_check CHECK (
        carousel_fallback_set_at IS NOT NULL
        OR (NOT carousel_fallback AND carousel_fallback_set_by IS NULL)
    );

CREATE INDEX conversation_carousel_fallback_set_by_idx
    ON conversation (agency_id, carousel_fallback_set_by)
    WHERE carousel_fallback_set_by IS NOT NULL;


-- The payloads a turn sends, stored once before its first send: a retried or
-- resumed turn sends exactly these under the same "turn:{id}:{seq}" keys, even if
-- the fallback flag or a listing's cover changed in between. NULL = not planned.
ALTER TABLE conversation_turn
    ADD COLUMN reply_plan JSONB CHECK (jsonb_typeof(reply_plan) = 'array');
