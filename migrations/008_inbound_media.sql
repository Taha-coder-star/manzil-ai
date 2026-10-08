-- Task 4.1: inbound media (component C5, D14) and the property_media columns of
-- D11 1.3 (gap G16 in docs/SPEC_ALIGNMENT.md). Additive: new columns are
-- nullable or have a constant default, so no row is changed by an UPDATE.
-- Design: docs/MEDIA.md. Run as the schema owner, never as the runtime login.
-- Requires 002, 005, 007.


-- A staff photo, fetched from WhatsApp and stored in the bucket before any
-- listing exists. Draft saving (Task 9.2) turns it into property_media; the
-- objects are referenced by key and never moved.
CREATE TABLE inbound_media (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    message_id UUID NOT NULL,
    staff_id UUID NOT NULL,
    -- The inbound WhatsApp media ID (valid for days; its download URL for 5 min).
    wa_media_id TEXT NOT NULL CHECK (btrim(wa_media_id) <> ''),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'stored', 'duplicate', 'rejected', 'failed')),
    error_code TEXT CHECK (btrim(error_code) <> ''),
    -- Unguessable path segment of the public card object; never an internal ID.
    card_token UUID NOT NULL DEFAULT gen_random_uuid(),
    -- Hash of the bytes as WhatsApp delivered them (dedupe key).
    content_sha256 TEXT CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    mime_type TEXT CHECK (mime_type IN ('image/jpeg', 'image/png', 'image/webp')),
    byte_size INTEGER CHECK (byte_size > 0),
    width INTEGER CHECK (width > 0),
    height INTEGER CHECK (height > 0),
    original_key TEXT CHECK (btrim(original_key) <> ''),
    wa_image_key TEXT CHECK (btrim(wa_image_key) <> ''),
    card_image_key TEXT CHECK (btrim(card_image_key) <> ''),
    public_url_card TEXT CHECK (public_url_card ~ '^https://'),
    exif_stripped BOOLEAN NOT NULL DEFAULT FALSE,
    -- The earlier stored copy of the same bytes; its objects are reused.
    duplicate_of_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    stored_at TIMESTAMPTZ,

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, message_id),
    UNIQUE (card_token),

    FOREIGN KEY (agency_id, conversation_id, message_id)
        REFERENCES message(agency_id, conversation_id, id),

    FOREIGN KEY (agency_id, staff_id)
        REFERENCES staff(agency_id, id),

    FOREIGN KEY (agency_id, duplicate_of_id)
        REFERENCES inbound_media(agency_id, id),

    CHECK (duplicate_of_id IS DISTINCT FROM id),

    -- Usable media always has its three objects, clean of EXIF.
    CONSTRAINT inbound_media_usable_check CHECK (
        status NOT IN ('stored', 'duplicate')
        OR (
            content_sha256 IS NOT NULL AND mime_type IS NOT NULL
            AND original_key IS NOT NULL AND wa_image_key IS NOT NULL
            AND card_image_key IS NOT NULL AND public_url_card IS NOT NULL
            AND exif_stripped AND stored_at IS NOT NULL
        )
    ),
    CONSTRAINT inbound_media_duplicate_check CHECK (
        (status = 'duplicate') = (duplicate_of_id IS NOT NULL)
    ),
    CONSTRAINT inbound_media_error_check CHECK (
        (status IN ('rejected', 'failed')) = (error_code IS NOT NULL)
    )
);

-- One stored copy per content hash and agency; later copies are duplicates.
CREATE UNIQUE INDEX inbound_media_stored_hash_key
    ON inbound_media (agency_id, content_sha256)
    WHERE status = 'stored';

-- A staff member's recent photos (segment grouping, Task 7.4).
CREATE INDEX inbound_media_staff_idx
    ON inbound_media (agency_id, staff_id, created_at);

CREATE INDEX inbound_media_conversation_idx
    ON inbound_media (agency_id, conversation_id, message_id);

CREATE INDEX inbound_media_duplicate_idx
    ON inbound_media (agency_id, duplicate_of_id);


-- D11 1.3 (G16). position = sort_order, storage_key_* = *_key and content_hash =
-- content_sha256 already exist (005). wa_media_id is the 30-day upload cache of
-- Task 4.2, scoped to the agency's one business number (D3).
ALTER TABLE property_media
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'image'
        CHECK (kind IN ('image', 'video', 'doc')),
    ADD COLUMN label TEXT
        CHECK (label IN (
            'exterior', 'lounge', 'kitchen', 'bedroom', 'bath', 'view', 'map',
            'floor_plan'
        )),
    ADD COLUMN public_url_card TEXT CHECK (public_url_card ~ '^https://'),
    ADD COLUMN wa_media_id TEXT CHECK (btrim(wa_media_id) <> ''),
    ADD COLUMN wa_media_expires_at TIMESTAMPTZ,
    ADD COLUMN exif_stripped BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN source_inbound_media_id UUID,
    ADD CONSTRAINT property_media_wa_media_pair_check CHECK (
        (wa_media_id IS NULL) = (wa_media_expires_at IS NULL)
    ),
    ADD CONSTRAINT property_media_source_inbound_media_fkey
        FOREIGN KEY (agency_id, source_inbound_media_id)
        REFERENCES inbound_media(agency_id, id);

CREATE INDEX property_media_source_inbound_media_idx
    ON property_media (agency_id, source_inbound_media_id);


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

    EXECUTE format('REVOKE ALL ON TABLE %I.inbound_media FROM PUBLIC, manzil_app',
                   target_schema);
    EXECUTE format('ALTER TABLE %I.inbound_media ENABLE ROW LEVEL SECURITY',
                   target_schema);
    EXECUTE format('ALTER TABLE %I.inbound_media FORCE ROW LEVEL SECURITY',
                   target_schema);
    EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I.inbound_media FOR ALL TO manzil_app '
        'USING (agency_id = %I.current_agency_id()) '
        'WITH CHECK (agency_id = %I.current_agency_id())',
        target_schema, target_schema, target_schema
    );
    -- Rows end by status, never by DELETE.
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE ON TABLE %I.inbound_media TO manzil_app',
        target_schema
    );
END
$migration$;
