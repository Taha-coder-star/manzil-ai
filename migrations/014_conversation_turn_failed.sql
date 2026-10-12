-- Task 6.1: failed conversation turns and a durable attempt count (component C3).
-- status 'failed': a turn that will not be answered. app.orchestrator closes a turn
-- as 'failed' when its job will not retry (a permanent error, or the failure of its
-- last attempt), and when the turn is found open after MAX_TURN_ATTEMPTS attempts
-- (attempts that crashed leave no other trace). 'failed' is terminal and never a
-- reply: nothing is claimed to have been sent, and its messages keep their turn_id,
-- so they are never assembled into another turn.
-- attempts: how many times a job has started answering the turn; 1 when it is
-- assembled, incremented (and committed) each time a job resumes it, before any
-- work, so a crash cannot undercount it.
-- Additive: one NOT NULL column with a constant default (existing rows read 1,
-- no table rewrite) and a wider status CHECK. The completed_at rule, the one-open-
-- turn index, grants and FORCE RLS (010) are unchanged.
-- Run as the schema owner, never as the runtime login. Requires 010.


ALTER TABLE conversation_turn
    ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1
        CONSTRAINT conversation_turn_attempts_check CHECK (attempts > 0),
    DROP CONSTRAINT conversation_turn_status_check,
    ADD CONSTRAINT conversation_turn_status_check
        CHECK (status IN ('open', 'replied', 'gated', 'failed'));
