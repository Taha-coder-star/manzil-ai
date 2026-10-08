-- Task 3.1: jobs of one conversation run one at a time.
-- NULL means the job needs no conversation lock (e.g. status reconciliation).
-- job already has ENABLE + FORCE RLS and the tenant_isolation policy (002).
ALTER TABLE job
    ADD COLUMN conversation_key TEXT
        CHECK (conversation_key IS NULL OR btrim(conversation_key) <> '');

CREATE INDEX job_conversation_active_idx
    ON job (agency_id, conversation_key)
    WHERE status IN ('pending', 'running');
