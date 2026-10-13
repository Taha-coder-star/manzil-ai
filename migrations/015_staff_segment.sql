-- Task 7.4: staff buffer segments (component C3 / C10; Red-Team R-08, R-09, R-10;
-- R3 staff pipeline step 3). A staff member's WhatsApp descriptions and photos are
-- grouped into segments: a new description text opens a new segment, photos attach
-- to the preceding description (or to the next one when none has arrived yet), and a
-- segment is flushed 75 s after its last item or on "done" / "bas". A flushed segment
-- is answered by one staff turn (conversation_turn.staff_segment_id); Task 8.2 reads
-- segments for draft extraction, Task 9.2 the photo-only ones (R-10 attach flow).
-- Design: docs/STAFF_BUFFER.md.
-- Additive: two new tables and one nullable column, so no row is changed.
-- Run as the schema owner, never as the runtime login. Requires 010.


CREATE TABLE staff_segment (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    staff_id UUID NOT NULL,
    -- The staff member's own WhatsApp conversation.
    conversation_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'flushed')),
    -- Set when flushed: a segment without a description is photo-only (R-10).
    kind TEXT CHECK (kind IN ('description', 'photo_only')),
    -- Bumped by every item; a flush job carries the version it was scheduled for,
    -- so an older timer finds a newer version and does nothing.
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    last_item_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    flush_due_at TIMESTAMPTZ NOT NULL,
    flushed_at TIMESTAMPTZ,
    flush_reason TEXT CHECK (flush_reason IN ('timer', 'done', 'next_description')),
    -- photo_only, media_pending, media_failed (docs/STAFF_BUFFER.md).
    flags JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(flags) = 'array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, conversation_id, id),

    FOREIGN KEY (agency_id, staff_id)
        REFERENCES staff(agency_id, id),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES conversation(agency_id, id),

    CONSTRAINT staff_segment_flushed_check CHECK (
        (status = 'flushed') = (flushed_at IS NOT NULL)
        AND (status = 'flushed') = (kind IS NOT NULL)
        AND (status = 'flushed') = (flush_reason IS NOT NULL)
    )
);

-- At most one open segment per staff member.
CREATE UNIQUE INDEX staff_segment_one_open_key
    ON staff_segment (agency_id, staff_id)
    WHERE status = 'open';

CREATE INDEX staff_segment_staff_idx
    ON staff_segment (agency_id, staff_id, created_at);


-- One row per inbound message of a segment, in order. A message belongs to at most
-- one segment, so a redelivered or replayed message is never added twice.
CREATE TABLE staff_segment_item (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    segment_id UUID NOT NULL,
    message_id UUID NOT NULL,
    position SMALLINT NOT NULL CHECK (position > 0),
    -- The role of the message's text: a property description, or a short note
    -- ("photos neeche hain"); NULL for a photo without a caption.
    text_role TEXT CHECK (text_role IN ('description', 'note')),
    is_photo BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, message_id),
    UNIQUE (agency_id, segment_id, position),

    FOREIGN KEY (agency_id, conversation_id, segment_id)
        REFERENCES staff_segment(agency_id, conversation_id, id),

    FOREIGN KEY (agency_id, conversation_id, message_id)
        REFERENCES message(agency_id, conversation_id, id),

    CHECK (text_role IS NOT NULL OR is_photo)
);

CREATE INDEX staff_segment_item_segment_idx
    ON staff_segment_item (agency_id, segment_id, position);


-- The staff turn that answers a flushed segment: its messages are the segment's
-- items (they already belong to the turns that buffered them). One turn per segment.
ALTER TABLE conversation_turn
    ADD COLUMN staff_segment_id UUID,
    ADD CONSTRAINT conversation_turn_staff_segment_fkey
        FOREIGN KEY (agency_id, conversation_id, staff_segment_id)
        REFERENCES staff_segment(agency_id, conversation_id, id),
    ADD CONSTRAINT conversation_turn_staff_segment_route_check
        CHECK (staff_segment_id IS NULL OR route = 'staff');

CREATE UNIQUE INDEX conversation_turn_staff_segment_key
    ON conversation_turn (agency_id, staff_segment_id)
    WHERE staff_segment_id IS NOT NULL;


-- Privileges and RLS follow the 002 / 005 conventions.
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

    FOREACH table_name IN ARRAY ARRAY['staff_segment', 'staff_segment_item'] LOOP
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
        -- Segments end by status and items are history: never DELETE.
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE %I.%I TO manzil_app',
                       target_schema, table_name);
    END LOOP;
END
$migration$;
