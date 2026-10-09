-- Task 4.2: outbound media (D14 point 4). Additive: new columns are nullable,
-- so no row is changed. Design: docs/MEDIA.md. Run as the schema owner.
-- Requires 001, 008.


-- Staged staff photos can be sent back (staff cards) before they become
-- property_media, so they carry the same 30-day upload cache as
-- property_media.wa_media_id / wa_media_expires_at (008, G16). Named wa_upload_*
-- because inbound_media.wa_media_id is the inbound ID the photo was fetched by.
-- IDs belong to the agency's one business number (D3).
ALTER TABLE inbound_media
    ADD COLUMN wa_upload_id TEXT CHECK (btrim(wa_upload_id) <> ''),
    ADD COLUMN wa_upload_expires_at TIMESTAMPTZ,
    ADD CONSTRAINT inbound_media_wa_upload_pair_check CHECK (
        (wa_upload_id IS NULL) = (wa_upload_expires_at IS NULL)
    );

-- The stored image an outbound image message showed, so a failed delivery
-- (status webhook) can clear the cached ID and resend the image by link.
-- Not referential: the source is one of two tables (cf. 006 JSON ids).
ALTER TABLE outbound_message
    ADD COLUMN media_source TEXT
        CHECK (media_source IN ('property_media', 'inbound_media')),
    ADD COLUMN media_source_id UUID,
    ADD CONSTRAINT outbound_message_media_source_pair_check CHECK (
        (media_source IS NULL) = (media_source_id IS NULL)
    );
