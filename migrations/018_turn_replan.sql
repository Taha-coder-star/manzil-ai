-- Day 12: retries retain the single supersede decision and dispatch boundary.
ALTER TABLE conversation_turn
    ADD COLUMN replan_count INTEGER NOT NULL DEFAULT 0 CHECK (replan_count IN (0, 1)),
    ADD COLUMN original_message_ids UUID[] NOT NULL DEFAULT '{}',
    ADD COLUMN dispatch_started BOOLEAN NOT NULL DEFAULT FALSE;
