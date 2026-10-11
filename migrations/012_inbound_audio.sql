-- Task 5.3: inbound audio (component C5, RFC voice flow step 2; gate M1(c)).
-- Every inbound audio message, whoever sent it, is downloaded once and kept
-- privately in the bucket as WhatsApp delivered it (no transcoding). Speech
-- (C4, Task 6.3) reads it from here; the R16 benchmark recordings are pulled
-- through the same path. Design: docs/MEDIA.md. Retention is short and set in
-- docs/DECISIONS.md. New table only: no existing row is changed.
-- Run as the schema owner, never as the runtime login. Requires 002, 006.


CREATE TABLE inbound_audio (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    message_id UUID NOT NULL,
    -- The inbound WhatsApp media ID (valid for days; its download URL for 5 min).
    wa_media_id TEXT NOT NULL CHECK (btrim(wa_media_id) <> ''),
    -- Meta's "voice" flag: true for a recorded voice note; NULL when not sent.
    is_voice BOOLEAN,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'stored', 'rejected', 'failed')),
    error_code TEXT CHECK (btrim(error_code) <> ''),
    -- The type found in the bytes (magic number), not the declared one.
    mime_type TEXT CHECK (mime_type IN (
        'audio/ogg', 'audio/mpeg', 'audio/mp4', 'audio/aac', 'audio/amr'
    )),
    content_sha256 TEXT CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size INTEGER CHECK (byte_size > 0),
    -- From the Ogg container (Opus voice notes); NULL for other formats.
    duration_ms INTEGER CHECK (duration_ms >= 0),
    -- Private object: tenant/<agency_id>/audio/<id>.<ext>; never under card/.
    storage_key TEXT CHECK (storage_key ~ '^tenant/'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    stored_at TIMESTAMPTZ,

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, message_id),

    FOREIGN KEY (agency_id, conversation_id, message_id)
        REFERENCES message(agency_id, conversation_id, id),

    CONSTRAINT inbound_audio_usable_check CHECK (
        status <> 'stored'
        OR (
            mime_type IS NOT NULL AND content_sha256 IS NOT NULL
            AND byte_size IS NOT NULL AND storage_key IS NOT NULL
            AND stored_at IS NOT NULL
        )
    ),
    CONSTRAINT inbound_audio_error_check CHECK (
        (status IN ('rejected', 'failed')) = (error_code IS NOT NULL)
    )
);

-- A conversation's voice notes in order (speech, benchmark manifest).
CREATE INDEX inbound_audio_conversation_idx
    ON inbound_audio (agency_id, conversation_id, created_at);


-- Privileges and RLS follow the 002 / 008 conventions.
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

    EXECUTE format('REVOKE ALL ON TABLE %I.inbound_audio FROM PUBLIC, manzil_app',
                   target_schema);
    EXECUTE format('ALTER TABLE %I.inbound_audio ENABLE ROW LEVEL SECURITY',
                   target_schema);
    EXECUTE format('ALTER TABLE %I.inbound_audio FORCE ROW LEVEL SECURITY',
                   target_schema);
    EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I.inbound_audio FOR ALL TO manzil_app '
        'USING (agency_id = %I.current_agency_id()) '
        'WITH CHECK (agency_id = %I.current_agency_id())',
        target_schema, target_schema, target_schema
    );
    -- Rows end by status, never by DELETE (the retention purge runs as the owner).
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE ON TABLE %I.inbound_audio TO manzil_app',
        target_schema
    );
END
$migration$;
