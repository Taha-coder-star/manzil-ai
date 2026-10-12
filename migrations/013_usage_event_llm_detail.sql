-- Task 6.1: LLM usage detail on usage_event (component C6, AI turn engine).
-- One usage_event row is written per provider attempt (primary and failover).
-- 001 has no field for the provider's cached-input or thinking token counts, and
-- outcome is only 'succeeded' / 'failed' (the provider call, not the task), so:
--   cached_input_tokens: input tokens the provider reports as served from its
--     prompt cache (Gemini usageMetadata.cachedContentTokenCount); NULL = not
--     reported. Part of input_tokens, never more than it.
--   thinking_tokens: reasoning tokens the provider reports separately
--     (Gemini thoughtsTokenCount); NULL = not reported.
--   outcome_detail: a sanitised diagnostic code (e.g. 'stop', 'tool_calls',
--     'refused:safety', 'malformed:malformed_function_call', 'timeout',
--     'http_429'). Never message text, prompts or provider error bodies.
-- Additive: nullable columns with no default, so no row is rewritten or changed.
-- The runtime keeps its table-level usage_event grants (002); FORCE RLS is
-- untouched. Run as the schema owner, never as the runtime login. Requires 001.


ALTER TABLE usage_event
    ADD COLUMN cached_input_tokens BIGINT
        CONSTRAINT usage_event_cached_input_tokens_check
        CHECK (cached_input_tokens >= 0),
    ADD COLUMN thinking_tokens BIGINT
        CONSTRAINT usage_event_thinking_tokens_check
        CHECK (thinking_tokens >= 0),
    ADD COLUMN outcome_detail TEXT
        CONSTRAINT usage_event_outcome_detail_check
        CHECK (outcome_detail ~ '^[a-z0-9_.:-]{1,64}$'),
    ADD CONSTRAINT usage_event_cached_within_input_check CHECK (
        cached_input_tokens IS NULL
        OR input_tokens IS NULL
        OR cached_input_tokens <= input_tokens
    );
