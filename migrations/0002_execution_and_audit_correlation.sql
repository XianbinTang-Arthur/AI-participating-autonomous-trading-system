-- AATS migration 0002
-- Add execution decision correlation for deterministic replay and audit validation.

ALTER TABLE order_states
    ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);

UPDATE order_states
SET decision_id = COALESCE(NULLIF(payload ->> 'decision_id', ''), 'legacy_unknown')
WHERE decision_id IS NULL;

ALTER TABLE order_states
    ALTER COLUMN decision_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_order_states_decision_id ON order_states (decision_id);
