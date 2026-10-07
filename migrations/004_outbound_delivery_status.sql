-- Task 3.2: Meta delivery lifecycle for outbound messages.
-- status stays the send-pipeline state (pending -> sending -> sent / failed /
-- unknown / cancelled); delivery_status follows the statuses webhooks and only
-- moves forward (sent < delivered < read; failed is terminal).
-- outbound_message already has ENABLE + FORCE RLS and the tenant_isolation policy (002).
ALTER TABLE outbound_message
    ADD COLUMN delivery_status TEXT
        CHECK (delivery_status IN ('sent', 'delivered', 'read', 'failed')),
    ADD COLUMN delivered_at TIMESTAMPTZ,
    ADD COLUMN read_at TIMESTAMPTZ,
    ADD COLUMN failed_at TIMESTAMPTZ,
    ADD COLUMN delivery_error_code TEXT;
