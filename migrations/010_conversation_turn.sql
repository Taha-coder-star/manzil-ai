-- Task 5.1: conversation turns (component C3, RFC flow step 2-3, Red-Team R-01).
-- One turn coalesces every pending inbound message of a conversation; the
-- message rows point at their turn, so a retried or crashed job resumes the
-- same turn instead of consuming its messages twice. Design: docs/STATE_SCHEMA.md.
-- Additive: one new table and one nullable column, so no row is changed.
-- Run as the schema owner, never as the runtime login. Requires 002 and 006.


CREATE TABLE conversation_turn (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    -- The process_message job that assembled the turn.
    trigger_job_id UUID NOT NULL,
    -- Routing snapshot from the sender allowlist (never from message content).
    sender_role TEXT NOT NULL
        CHECK (sender_role IN ('customer', 'admin', 'agent', 'broker')),
    route TEXT NOT NULL CHECK (route IN ('customer', 'staff', 'broker')),
    staff_id UUID,
    -- Control snapshot the gate decided on.
    control_state TEXT NOT NULL
        CHECK (control_state IN ('ai_active', 'handoff_pending', 'human_owned', 'closed')),
    conversation_mode TEXT NOT NULL CHECK (conversation_mode IN ('ai', 'human', 'paused')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'replied', 'gated')),
    message_count INTEGER NOT NULL CHECK (message_count > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, conversation_id, id),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES conversation(agency_id, id),

    FOREIGN KEY (agency_id, trigger_job_id)
        REFERENCES job(agency_id, id),

    FOREIGN KEY (agency_id, staff_id)
        REFERENCES staff(agency_id, id),

    CONSTRAINT conversation_turn_route_role_check CHECK (
        route = CASE sender_role
            WHEN 'customer' THEN 'customer'
            WHEN 'broker' THEN 'broker'
            ELSE 'staff'
        END
    ),
    CONSTRAINT conversation_turn_staff_check CHECK (
        (sender_role = 'customer') = (staff_id IS NULL)
    ),
    CONSTRAINT conversation_turn_completed_check CHECK (
        (status = 'open') = (completed_at IS NULL)
    )
);

-- At most one unfinished turn per conversation: the next job resumes it.
CREATE UNIQUE INDEX conversation_turn_one_open_key
    ON conversation_turn (agency_id, conversation_id)
    WHERE status = 'open';

CREATE INDEX conversation_turn_conversation_idx
    ON conversation_turn (agency_id, conversation_id, created_at);

CREATE INDEX conversation_turn_trigger_job_idx
    ON conversation_turn (agency_id, trigger_job_id);

CREATE INDEX conversation_turn_staff_idx
    ON conversation_turn (agency_id, staff_id);


-- The turn that consumed an inbound message; NULL = not yet part of a turn.
-- The composite key keeps a message inside its own conversation's turns.
ALTER TABLE message
    ADD COLUMN turn_id UUID,
    ADD CONSTRAINT message_turn_fkey
        FOREIGN KEY (agency_id, conversation_id, turn_id)
        REFERENCES conversation_turn(agency_id, conversation_id, id),
    ADD CONSTRAINT message_turn_inbound_check
        CHECK (turn_id IS NULL OR direction = 'inbound');

CREATE INDEX message_turn_idx
    ON message (agency_id, turn_id)
    WHERE turn_id IS NOT NULL;


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

    EXECUTE format('REVOKE ALL ON TABLE %I.conversation_turn FROM PUBLIC, manzil_app',
                   target_schema);
    EXECUTE format('ALTER TABLE %I.conversation_turn ENABLE ROW LEVEL SECURITY',
                   target_schema);
    EXECUTE format('ALTER TABLE %I.conversation_turn FORCE ROW LEVEL SECURITY',
                   target_schema);
    EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I.conversation_turn FOR ALL TO manzil_app '
        'USING (agency_id = %I.current_agency_id()) '
        'WITH CHECK (agency_id = %I.current_agency_id())',
        target_schema, target_schema, target_schema
    );
    -- Turns end by status, never by DELETE.
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE ON TABLE %I.conversation_turn TO manzil_app',
        target_schema
    );
END
$migration$;
