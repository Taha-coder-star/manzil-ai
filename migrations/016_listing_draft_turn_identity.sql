-- Task 8.2 slice 4a: retry identity for staff drafts and the `superseded` draft status
-- (docs/STAFF_EXTRACTION.md section 5; Red-Team R-12 / R-14; SPEC_ALIGNMENT G17).
-- A staff turn stores its create drafts once: (agency_id, source_turn_id, ordinal) is the
-- identity, so a retried turn finds its drafts instead of making new ones. A newer update
-- proposal for the same staff member and listing marks the older draft `superseded`.
-- Conflicts live in the payload and the segment is reached through the source turn
-- (conversation_turn.staff_segment_id), so neither gets a column.
-- Additive: two nullable columns, one status added to a CHECK, one FK, one partial unique
-- index. Nothing writes listing_draft before this task, so no row is checked or changed.
-- No new table: the grants and the tenant_isolation policy of 005 already cover the columns.
-- Run as the schema owner, never as the runtime login. Requires 005, 010.


-- 005's status CHECK is replaced below. It must still be the unnamed 005 constraint; a
-- schema that has drifted fails here instead of silently losing a rule.
DO $replace$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_constraint
        WHERE conrelid = format('%I.%I', current_schema(), 'listing_draft')::regclass
          AND conname = 'listing_draft_status_check'
          AND contype = 'c'
    ) THEN
        RAISE EXCEPTION 'expected constraint listing_draft.listing_draft_status_check is missing';
    END IF;
    ALTER TABLE listing_draft DROP CONSTRAINT listing_draft_status_check;
END
$replace$;

ALTER TABLE listing_draft
    ADD CONSTRAINT listing_draft_status_check
        CHECK (status IN ('open', 'saved', 'cancelled', 'superseded')),

    -- The staff turn that created the draft and the draft's place in it (1-5, R-08).
    ADD COLUMN source_turn_id UUID,
    ADD COLUMN ordinal SMALLINT,

    -- Same agency as the draft: another agency's turn cannot be referenced.
    ADD CONSTRAINT listing_draft_source_turn_fkey
        FOREIGN KEY (agency_id, source_turn_id)
        REFERENCES conversation_turn(agency_id, id),

    ADD CONSTRAINT listing_draft_ordinal_check CHECK (ordinal BETWEEN 1 AND 5),

    -- A turn and an ordinal come together or not at all.
    ADD CONSTRAINT listing_draft_turn_pair_check
        CHECK ((source_turn_id IS NULL) = (ordinal IS NULL));

-- The retry identity: one draft per turn and ordinal.
CREATE UNIQUE INDEX listing_draft_turn_ordinal_key
    ON listing_draft (agency_id, source_turn_id, ordinal)
    WHERE source_turn_id IS NOT NULL;
