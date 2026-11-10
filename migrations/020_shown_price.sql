-- Price actually rendered in a delivered search card, not a later inventory read.
-- Existing history has no trustworthy price snapshot and remains NULL.
ALTER TABLE shown_item ADD COLUMN rendered_price_pkr BIGINT
    CHECK (rendered_price_pkr IS NULL OR rendered_price_pkr >= 0);
