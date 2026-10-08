-- Task 3.4: conversation state, search memory and handoff schema.
-- Design and assumptions: docs/STATE_SCHEMA.md. Run as the schema owner, never
-- as the runtime login. Requires migrations 002 (manzil_app, current_agency_id())
-- and 005 (property, property_media). Schema only: the send gate still reads
-- conversation.mode (app/outbound.py).


-- Conversation control state. mode and ai_muted_until stay as they are.
ALTER TABLE conversation
    ADD COLUMN control_state TEXT NOT NULL DEFAULT 'ai_active'
        CHECK (
            control_state IN ('ai_active', 'handoff_pending', 'human_owned', 'closed')
        ),
    ADD COLUMN preferred_language TEXT
        CHECK (preferred_language IN ('english', 'roman_urdu', 'urdu')),
    ADD COLUMN last_modality TEXT
        CHECK (last_modality IN ('text', 'voice'));

-- Backfill: ai -> ai_active; human and paused -> human_owned (never an AI state).
-- FORCE RLS hides every row from the owner, which has no policy, so an UPDATE
-- would silently change nothing. FORCE is lifted only inside this transaction:
-- ALTER TABLE holds an ACCESS EXCLUSIVE lock until commit, so no other session
-- can observe it, and it is restored before the block ends.
DO $backfill$
BEGIN
    ALTER TABLE conversation NO FORCE ROW LEVEL SECURITY;
    UPDATE conversation SET control_state = 'human_owned' WHERE mode IN ('human', 'paused');
    IF EXISTS (
        SELECT FROM conversation WHERE mode <> 'ai' AND control_state = 'ai_active'
    ) THEN
        RAISE EXCEPTION 'conversation control_state backfill is incomplete';
    END IF;
    ALTER TABLE conversation FORCE ROW LEVEL SECURITY;
END
$backfill$;

-- A conversation the control state silences can never look AI-enabled to the
-- current mode-based send gate. (mode alone may still silence an ai_active row.)
ALTER TABLE conversation
    ADD CONSTRAINT conversation_control_state_gate_check
        CHECK (control_state = 'ai_active' OR mode <> 'ai');


-- Message extensions. modality is derived, so rows written by the existing
-- ingestion code are labelled correctly without any code change.
ALTER TABLE message
    ADD COLUMN modality TEXT GENERATED ALWAYS AS (
        CASE message_type WHEN 'text' THEN 'text' WHEN 'audio' THEN 'voice' END
    ) STORED,
    ADD COLUMN transcript TEXT,
    ADD COLUMN stt_meta JSONB CHECK (jsonb_typeof(stt_meta) = 'object'),
    -- WAMID of the message this one quotes (WhatsApp context.id).
    ADD COLUMN context_wamid TEXT CHECK (btrim(context_wamid) <> ''),
    ADD CONSTRAINT message_transcript_inbound_voice_check CHECK (
        (transcript IS NULL AND stt_meta IS NULL)
        OR (direction = 'inbound' AND message_type = 'audio')
    );


-- Per-agency lead numbers; allocated by next_lead_number() below.
ALTER TABLE agency
    ADD COLUMN next_lead_number INTEGER NOT NULL DEFAULT 1
        CHECK (next_lead_number > 0);


-- Customer requirements: one versioned row per conversation.
CREATE TABLE requirement_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    -- 0 = nothing captured yet; every committed patch increments it.
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    -- slot -> {value, raw, source, confidence, confirmed, updated_at}; absent = unknown.
    slots JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, conversation_id),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES conversation(agency_id, id),

    CONSTRAINT requirement_state_slots_check CHECK (
        jsonb_typeof(slots) = 'object'
        AND slots - ARRAY[
            'purpose', 'category', 'property_types', 'location', 'budget',
            'previous_budget', 'size', 'beds_min', 'baths_min', 'construction',
            'possession', 'investment', 'soft_prefs', 'unmapped_prefs',
            'excluded_property_ids'
        ] = '{}'::jsonb
        AND NOT jsonb_path_exists(slots, '$.*.type() ? (@ != "object")')
        AND jsonb_array_length(jsonb_path_query_array(slots, '$.*.source'))
            = jsonb_array_length(jsonb_path_query_array(slots, '$.*'))
        AND jsonb_path_query_array(slots, '$.*.source')
            <@ '["text", "voice", "inferred", "default"]'::jsonb
        AND NOT jsonb_path_exists(slots, '$.*.confidence.type() ? (@ != "number")')
        AND NOT jsonb_path_exists(slots, '$.*.confidence ? (@ < 0 || @ > 1)')
        AND NOT jsonb_path_exists(slots, '$.*.confirmed.type() ? (@ != "boolean")')
    )
);


-- Append-only history of requirement patches (undo, evaluation). Each op is
-- {slot, op, raw_span, ...} (R17 2.1); old values for undo go in the op.
CREATE TABLE requirement_patch_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    -- The requirement_state version this patch produced.
    version INTEGER NOT NULL CHECK (version > 0),
    ops JSONB NOT NULL,
    source_message_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, conversation_id, version),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES requirement_state(agency_id, conversation_id),

    FOREIGN KEY (agency_id, conversation_id, source_message_id)
        REFERENCES message(agency_id, conversation_id, id),

    CONSTRAINT requirement_patch_log_ops_check CHECK (
        jsonb_typeof(ops) = 'array'
        AND jsonb_array_length(ops) > 0
        AND NOT jsonb_path_exists(ops, '$[*].type() ? (@ != "object")')
        AND jsonb_array_length(jsonb_path_query_array(ops, '$[*].op'))
            = jsonb_array_length(ops)
        AND jsonb_path_query_array(ops, '$[*].op')
            <@ '["set", "add", "remove", "clear", "relative"]'::jsonb
        AND jsonb_array_length(jsonb_path_query_array(ops, '$[*].slot'))
            = jsonb_array_length(ops)
        AND jsonb_path_query_array(ops, '$[*].slot') <@ '[
            "purpose", "category", "property_types", "location", "budget",
            "previous_budget", "size", "beds_min", "baths_min", "construction",
            "possession", "investment", "soft_prefs", "unmapped_prefs",
            "excluded_property_ids"
        ]'::jsonb
    )
);


-- One executed search; the requirement version must exist for this conversation.
CREATE TABLE search_run (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    requirement_version INTEGER NOT NULL,
    filters JSONB NOT NULL CHECK (jsonb_typeof(filters) = 'object'),
    relaxations JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(relaxations) = 'array'),
    total_matches INTEGER NOT NULL CHECK (total_matches >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, conversation_id, id),

    FOREIGN KEY (agency_id, conversation_id, requirement_version)
        REFERENCES requirement_patch_log(agency_id, conversation_id, version)
);


-- Listings shown by a search run, by position ("Property 2", quoted cards).
-- The card's WAMID is message.provider_message_id of card_message_id.
CREATE TABLE shown_item (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    search_run_id UUID NOT NULL,
    position SMALLINT NOT NULL CHECK (position > 0),
    property_id UUID NOT NULL,
    card_message_id UUID,
    shown_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, search_run_id, position),
    UNIQUE (agency_id, search_run_id, property_id),

    FOREIGN KEY (agency_id, conversation_id, search_run_id)
        REFERENCES search_run(agency_id, conversation_id, id),

    FOREIGN KEY (agency_id, property_id)
        REFERENCES property(agency_id, id),

    FOREIGN KEY (agency_id, conversation_id, card_message_id)
        REFERENCES message(agency_id, conversation_id, id)
);


-- The listing a customer conversation is currently about. Staff working
-- context is staff_session.focus_property_id, not this table.
CREATE TABLE focus (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    property_id UUID,
    selected_via TEXT
        CHECK (selected_via IN ('button', 'position', 'quote', 'description')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, conversation_id),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES conversation(agency_id, id),

    FOREIGN KEY (agency_id, property_id)
        REFERENCES property(agency_id, id),

    CHECK ((property_id IS NULL) = (selected_via IS NULL))
);


-- Listing photos already sent in a conversation ("aur pictures" without repeats).
CREATE TABLE media_sent (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    property_id UUID NOT NULL,
    -- NULL once the photo itself has been deleted; the record of the send stays.
    property_media_id UUID,
    message_id UUID,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, conversation_id, property_media_id),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES conversation(agency_id, id),

    FOREIGN KEY (agency_id, property_id)
        REFERENCES property(agency_id, id),

    -- The photo must belong to the listing recorded with it.
    FOREIGN KEY (agency_id, property_id, property_media_id)
        REFERENCES property_media(agency_id, property_id, id)
        ON DELETE SET NULL (property_media_id),

    FOREIGN KEY (agency_id, conversation_id, message_id)
        REFERENCES message(agency_id, conversation_id, id)
);


-- An open question to the customer; searching waits while one is open.
CREATE TABLE pending_clarification (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    slot TEXT NOT NULL CHECK (slot IN ('location', 'purpose', 'target')),
    candidates JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(candidates) = 'array'),
    asked_message_id UUID,
    -- Customer turns that did not answer; re-ask once, expire after 2 (R17 edge 11).
    unanswered_turns SMALLINT NOT NULL DEFAULT 0 CHECK (unanswered_turns >= 0),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'expired', 'cancelled')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,

    UNIQUE (agency_id, id),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES conversation(agency_id, id),

    FOREIGN KEY (agency_id, conversation_id, asked_message_id)
        REFERENCES message(agency_id, conversation_id, id),

    CHECK ((status = 'open') = (closed_at IS NULL)),
    CHECK (expires_at > created_at)
);


-- A customer lead for brokers. lead_number comes from next_lead_number().
CREATE TABLE lead (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    lead_number INTEGER NOT NULL CHECK (lead_number > 0),
    -- Broker-facing code (#done L-07); at least two digits, never truncated.
    code TEXT GENERATED ALWAYS AS (
        'L-' || CASE WHEN lead_number < 10 THEN '0' ELSE '' END || lead_number::text
    ) STORED,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    requirement_version INTEGER,
    requirement_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(requirement_snapshot) = 'object'),
    last_customer_message_id UUID,
    reason TEXT CHECK (btrim(reason) <> ''),
    summary TEXT CHECK (btrim(summary) <> ''),
    assigned_staff_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, conversation_id, id),
    UNIQUE (agency_id, lead_number),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES conversation(agency_id, id),

    FOREIGN KEY (agency_id, conversation_id, requirement_version)
        REFERENCES requirement_patch_log(agency_id, conversation_id, version),

    FOREIGN KEY (agency_id, conversation_id, last_customer_message_id)
        REFERENCES message(agency_id, conversation_id, id),

    FOREIGN KEY (agency_id, assigned_staff_id)
        REFERENCES staff(agency_id, id),

    CHECK ((status = 'open') = (closed_at IS NULL))
);


-- Properties a lead is about: the confirmed one(s), e.g. both after "Dono" (R-31).
-- Viewed properties are already recorded in shown_item.
CREATE TABLE lead_property (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    lead_id UUID NOT NULL,
    property_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, lead_id, property_id),

    FOREIGN KEY (agency_id, lead_id)
        REFERENCES lead(agency_id, id),

    FOREIGN KEY (agency_id, property_id)
        REFERENCES property(agency_id, id)
);


-- One handoff attempt: pending (accept timer) -> accepted (human owns) ->
-- completed (#done) / released (48 h) / cancelled.
CREATE TABLE handoff (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    lead_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'completed', 'released', 'cancelled')),
    assigned_staff_id UUID,
    accepted_by_staff_id UUID,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accept_due_at TIMESTAMPTZ,
    escalated_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    release_due_at TIMESTAMPTZ,
    -- Set once with UPDATE ... WHERE holding_reply_sent_at IS NULL (one per handoff).
    holding_reply_sent_at TIMESTAMPTZ,
    -- Broker buttons: [Called], then the outcome for ROI tracking.
    called_at TIMESTAMPTZ,
    outcome TEXT CHECK (outcome IN ('visit_booked', 'not_interested', 'deal')),
    outcome_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),

    -- The lead must belong to the same conversation.
    FOREIGN KEY (agency_id, conversation_id, lead_id)
        REFERENCES lead(agency_id, conversation_id, id),

    FOREIGN KEY (agency_id, assigned_staff_id)
        REFERENCES staff(agency_id, id),

    FOREIGN KEY (agency_id, accepted_by_staff_id)
        REFERENCES staff(agency_id, id),

    CHECK ((status IN ('pending', 'accepted')) = (closed_at IS NULL)),
    CHECK ((accepted_at IS NULL) = (accepted_by_staff_id IS NULL)),
    CHECK (status NOT IN ('accepted', 'completed') OR accepted_at IS NOT NULL),
    CHECK (status <> 'pending' OR accepted_at IS NULL),
    CHECK (accept_due_at IS NULL OR accept_due_at > requested_at),
    CHECK (release_due_at IS NULL OR release_due_at > requested_at),
    CHECK (escalated_at IS NULL OR escalated_at >= requested_at),
    CHECK (accepted_at IS NULL OR accepted_at >= requested_at),
    CHECK (holding_reply_sent_at IS NULL OR holding_reply_sent_at >= requested_at),
    CHECK (called_at IS NULL OR called_at >= requested_at),
    CHECK ((outcome IS NULL) = (outcome_at IS NULL)),
    CHECK (outcome_at IS NULL OR outcome_at >= requested_at),
    CHECK (closed_at IS NULL OR closed_at >= requested_at)
);


-- "One open X per conversation" invariants.
CREATE UNIQUE INDEX pending_clarification_one_open_idx
    ON pending_clarification (agency_id, conversation_id)
    WHERE status = 'open';

CREATE UNIQUE INDEX lead_one_open_idx
    ON lead (agency_id, conversation_id)
    WHERE status = 'open';

CREATE UNIQUE INDEX handoff_one_open_idx
    ON handoff (agency_id, conversation_id)
    WHERE status IN ('pending', 'accepted');


-- Lookups and foreign keys to rows the runtime may delete or reassign.
CREATE INDEX requirement_patch_log_message_idx
    ON requirement_patch_log (agency_id, conversation_id, source_message_id);

CREATE INDEX search_run_latest_idx
    ON search_run (agency_id, conversation_id, created_at DESC);

CREATE INDEX shown_item_property_idx
    ON shown_item (agency_id, property_id);

CREATE INDEX shown_item_card_message_idx
    ON shown_item (agency_id, conversation_id, card_message_id);

CREATE INDEX focus_property_idx
    ON focus (agency_id, property_id);

CREATE INDEX media_sent_media_idx
    ON media_sent (agency_id, property_id, property_media_id);

CREATE INDEX media_sent_message_idx
    ON media_sent (agency_id, conversation_id, message_id);

CREATE INDEX pending_clarification_message_idx
    ON pending_clarification (agency_id, conversation_id, asked_message_id);

CREATE INDEX lead_property_property_idx
    ON lead_property (agency_id, property_id);

CREATE INDEX lead_message_idx
    ON lead (agency_id, conversation_id, last_customer_message_id);

CREATE INDEX lead_assigned_open_idx
    ON lead (agency_id, assigned_staff_id)
    WHERE status = 'open';

CREATE INDEX handoff_lead_idx
    ON handoff (agency_id, conversation_id, lead_id);

CREATE INDEX handoff_assigned_open_idx
    ON handoff (agency_id, assigned_staff_id)
    WHERE status IN ('pending', 'accepted');

CREATE INDEX handoff_accepted_by_idx
    ON handoff (agency_id, accepted_by_staff_id);


-- Allocator, privileges and RLS follow the 002 / 005 conventions.
DO $migration$
DECLARE
    target_schema TEXT := current_schema();
    app_role RECORD;
    table_name TEXT;
BEGIN
    SELECT * INTO app_role
    FROM pg_catalog.pg_roles WHERE rolname = 'manzil_app';

    IF NOT FOUND OR app_role.rolsuper OR app_role.rolbypassrls THEN
        RAISE EXCEPTION 'manzil_app is missing or has unsafe attributes';
    END IF;

    -- Runs as the caller under RLS: without tenant context it updates no row and
    -- returns NULL, so the lead insert fails. The agency row lock serialises
    -- concurrent allocations; a rollback returns the number (gapless).
    EXECUTE format($function$
        CREATE FUNCTION %1$I.next_lead_number() RETURNS INTEGER
        LANGUAGE SQL VOLATILE SECURITY INVOKER
        SET search_path = pg_catalog
        AS $body$
            UPDATE %1$I.agency SET next_lead_number = next_lead_number + 1
            WHERE id = %1$I.current_agency_id()
            RETURNING next_lead_number - 1
        $body$
    $function$, target_schema);
    EXECUTE format(
        'REVOKE ALL ON FUNCTION %I.next_lead_number() FROM PUBLIC', target_schema
    );
    EXECUTE format(
        'GRANT EXECUTE ON FUNCTION %I.next_lead_number() TO manzil_app', target_schema
    );

    FOREACH table_name IN ARRAY ARRAY[
        'requirement_state', 'requirement_patch_log', 'search_run', 'shown_item',
        'focus', 'media_sent', 'pending_clarification', 'lead', 'lead_property',
        'handoff'
    ] LOOP
        EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM PUBLIC, manzil_app',
                       target_schema, table_name);
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
                       target_schema, table_name);
        EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY',
                       target_schema, table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I.%I FOR ALL TO manzil_app '
            'USING (agency_id = %I.current_agency_id()) '
            'WITH CHECK (agency_id = %I.current_agency_id())',
            target_schema, table_name, target_schema, target_schema
        );
    END LOOP;

    -- Histories are append-only for the runtime.
    EXECUTE format(
        'GRANT SELECT, INSERT ON TABLE %I.requirement_patch_log, %I.search_run, '
        '%I.shown_item, %I.lead_property TO manzil_app',
        target_schema, target_schema, target_schema, target_schema
    );
    -- Mutable state; rows end by status, never by DELETE.
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE ON TABLE %I.requirement_state, %I.focus, '
        '%I.media_sent, %I.pending_clarification, %I.lead, %I.handoff TO manzil_app',
        target_schema, target_schema, target_schema, target_schema, target_schema,
        target_schema
    );
END
$migration$;
