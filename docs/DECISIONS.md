# Decisions

- Agency is the tenant; v1 allows one connected WhatsApp business sender per agency to keep routing unambiguous.
- Composite foreign keys include agency_id to prevent cross-tenant relationships; outbound messages also bind the conversation and message together.
- Applied migrations are immutable and checksum-verified to detect drift; migration runs are transactional and serialized.
- Task 2.2 uses ENABLE and FORCE RLS on all nine application tables so table ownership alone cannot bypass policies.
- Runtime manzil_app must be a non-owner with no superuser, BYPASSRLS, or elevated role memberships; migration administration stays separate.
- Missing tenant context returns zero rows by design; trusted routing/discovery must supply context explicitly.
- Tenant context is UUID-validated and transaction-local; arbitrary customer input is not tenant authority.
- Reject nested/open transactions and session-level tenant context so connection reuse cannot silently inherit another tenant.
- Tests connect directly as the restricted login and CI requires PostgreSQL so bypass-role tests or skipped checks cannot masquerade as isolation proof.
- Reject unsafe existing role capabilities, memberships, ownership, and policies rather than silently widening runtime access.
- Keep estimated usage measurements null when unknown; do not invent zero costs or usage.
- updated_at is maintained by application code; schema v1 has no automatic timestamp trigger.
- Provider choices from handoff are Gemini for text/audio fallback and ElevenLabs Scribe for primary transcription; keys and current provider behavior remain unverified locally.
- Webhook callback path is /webhook. Signature is verified over raw bytes before parsing; missing app secret fails closed with 503; authentic but unusable payloads get 200 (retries cannot fix them). The verify token is redacted from access logs.
