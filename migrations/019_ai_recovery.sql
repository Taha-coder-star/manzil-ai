-- One guarded recovery per failed source turn; the original plan stays immutable.
CREATE TABLE ai_recovery (
    agency_id UUID NOT NULL,
    source_turn_id UUID NOT NULL,
    generation INTEGER NOT NULL CHECK (generation IN (0, 1)),
    message_ids UUID[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'planning', 'ready', 'completed', 'cancelled', 'failed')),
    reply_plan JSONB CHECK (jsonb_typeof(reply_plan) = 'array'),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (agency_id, source_turn_id),
    FOREIGN KEY (agency_id, source_turn_id) REFERENCES conversation_turn(agency_id, id)
);
ALTER TABLE ai_recovery ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_recovery FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ai_recovery FOR ALL TO manzil_app
    USING (agency_id = current_agency_id()) WITH CHECK (agency_id = current_agency_id());
REVOKE ALL ON ai_recovery FROM PUBLIC, manzil_app;
GRANT SELECT, INSERT, UPDATE ON ai_recovery TO manzil_app;
