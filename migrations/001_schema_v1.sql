CREATE TABLE agency (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    slug TEXT NOT NULL UNIQUE CHECK (btrim(slug) <> ''),
    meta_waba_id TEXT,
    meta_phone_number_id TEXT UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


CREATE TABLE staff (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    whatsapp_number TEXT NOT NULL
        CHECK (btrim(whatsapp_number) <> ''),
    role TEXT NOT NULL
        CHECK (role IN ('admin', 'agent', 'broker')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, whatsapp_number)
);


CREATE TABLE tenant_secret (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    secret_name TEXT NOT NULL CHECK (btrim(secret_name) <> ''),
    ciphertext BYTEA NOT NULL CHECK (octet_length(ciphertext) > 0),
    encryption_key_id TEXT NOT NULL
        CHECK (btrim(encryption_key_id) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, secret_name)
);


CREATE TABLE inbound_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    event_key TEXT NOT NULL CHECK (btrim(event_key) <> ''),
    event_type TEXT NOT NULL CHECK (btrim(event_type) <> ''),
    provider_message_id TEXT,
    payload JSONB NOT NULL
        CHECK (jsonb_typeof(payload) = 'object'),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, event_key)
);


CREATE TABLE job (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    inbound_event_id UUID,
    job_type TEXT NOT NULL CHECK (btrim(job_type) <> ''),
    dedupe_key TEXT NOT NULL CHECK (btrim(dedupe_key) <> ''),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending', 'running', 'succeeded', 'failed', 'dead'
            )
        ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3
        CHECK (max_attempts > 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error_code TEXT,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, dedupe_key),

    FOREIGN KEY (agency_id, inbound_event_id)
        REFERENCES inbound_event(agency_id, id),

    CHECK (
        status <> 'running'
        OR (
            locked_at IS NOT NULL
            AND locked_by IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    ),

    CHECK (
        status <> 'succeeded'
        OR completed_at IS NOT NULL
    )
);


CREATE TABLE conversation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    customer_wa_id TEXT NOT NULL
        CHECK (btrim(customer_wa_id) <> ''),
    mode TEXT NOT NULL DEFAULT 'ai'
        CHECK (mode IN ('ai', 'human', 'paused')),
    assigned_staff_id UUID,
    ai_muted_until TIMESTAMPTZ,
    last_customer_message_at TIMESTAMPTZ,
    state JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(state) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, customer_wa_id),

    FOREIGN KEY (agency_id, assigned_staff_id)
        REFERENCES staff(agency_id, id)
);


CREATE TABLE message (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    inbound_event_id UUID,
    provider_message_id TEXT,
    direction TEXT NOT NULL
        CHECK (direction IN ('inbound', 'outbound')),
    message_type TEXT NOT NULL
        CHECK (btrim(message_type) <> ''),
    text_body TEXT,
    media_id TEXT,
    provider_timestamp TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, conversation_id, id),
    UNIQUE (agency_id, provider_message_id),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES conversation(agency_id, id),

    FOREIGN KEY (agency_id, inbound_event_id)
        REFERENCES inbound_event(agency_id, id)
);


CREATE TABLE outbound_message (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    conversation_id UUID NOT NULL,
    message_id UUID NOT NULL,
    dedupe_key TEXT NOT NULL CHECK (btrim(dedupe_key) <> ''),
    recipient_wa_id TEXT NOT NULL
        CHECK (btrim(recipient_wa_id) <> ''),
    payload JSONB NOT NULL
        CHECK (jsonb_typeof(payload) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending', 'sending', 'sent',
                'failed', 'unknown', 'cancelled'
            )
        ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3
        CHECK (max_attempts > 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    provider_message_id TEXT,
    last_error_code TEXT,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, dedupe_key),
    UNIQUE (agency_id, message_id),
    UNIQUE (agency_id, provider_message_id),

    FOREIGN KEY (agency_id, conversation_id)
        REFERENCES conversation(agency_id, id),

    FOREIGN KEY (agency_id, conversation_id, message_id)
        REFERENCES message(agency_id, conversation_id, id),

    CHECK (
        status <> 'sending'
        OR (
            locked_by IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    ),

    CHECK (
        status <> 'sent'
        OR (
            provider_message_id IS NOT NULL
            AND sent_at IS NOT NULL
        )
    )
);


CREATE TABLE usage_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    job_id UUID,
    provider TEXT NOT NULL CHECK (btrim(provider) <> ''),
    model TEXT,
    operation TEXT NOT NULL CHECK (btrim(operation) <> ''),
    request_key TEXT NOT NULL CHECK (btrim(request_key) <> ''),
    input_tokens BIGINT CHECK (input_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens >= 0),
    audio_seconds NUMERIC(12,3) CHECK (audio_seconds >= 0),
    estimated_cost NUMERIC(18,8) CHECK (estimated_cost >= 0),
    currency TEXT CHECK (currency ~ '^[A-Z]{3}$'),
    outcome TEXT NOT NULL
        CHECK (outcome IN ('succeeded', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, provider, request_key),

    FOREIGN KEY (agency_id, job_id)
        REFERENCES job(agency_id, id),

    CHECK (
        estimated_cost IS NULL
        OR currency IS NOT NULL
    )
);


-- Webhook history and event lookup.
CREATE INDEX inbound_event_received_idx
    ON inbound_event (agency_id, received_at);

CREATE INDEX inbound_event_provider_message_idx
    ON inbound_event (agency_id, provider_message_id);


-- Jobs ready for claiming and expired worker leases.
CREATE INDEX job_ready_idx
    ON job (agency_id, available_at, created_at)
    WHERE status = 'pending';

CREATE INDEX job_expired_lease_idx
    ON job (agency_id, lease_expires_at)
    WHERE status = 'running';

CREATE INDEX job_inbound_event_idx
    ON job (agency_id, inbound_event_id);


-- Staff assignments and conversation history.
CREATE INDEX conversation_assigned_staff_idx
    ON conversation (agency_id, assigned_staff_id);

CREATE INDEX message_conversation_history_idx
    ON message (agency_id, conversation_id, created_at, id);

CREATE INDEX message_inbound_event_idx
    ON message (agency_id, inbound_event_id);


-- Replies ready for sending and expired sender leases.
CREATE INDEX outbound_message_ready_idx
    ON outbound_message (agency_id, available_at, created_at)
    WHERE status = 'pending';

CREATE INDEX outbound_message_expired_lease_idx
    ON outbound_message (agency_id, lease_expires_at)
    WHERE status = 'sending';

CREATE INDEX outbound_message_conversation_idx
    ON outbound_message (agency_id, conversation_id);


-- Usage reports and job lookup.
CREATE INDEX usage_event_agency_time_idx
    ON usage_event (agency_id, created_at);

CREATE INDEX usage_event_job_idx
    ON usage_event (agency_id, job_id);