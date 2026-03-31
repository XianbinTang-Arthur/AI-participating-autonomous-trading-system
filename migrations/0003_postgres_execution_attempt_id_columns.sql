ALTER TABLE execution_orders
    ADD COLUMN IF NOT EXISTS execution_attempt_id VARCHAR(128);

ALTER TABLE execution_fills
    ADD COLUMN IF NOT EXISTS execution_attempt_id VARCHAR(128);

ALTER TABLE fill_outcomes
    ADD COLUMN IF NOT EXISTS execution_attempt_id VARCHAR(128);

UPDATE execution_orders
SET execution_attempt_id = COALESCE(
    execution_attempt_id,
    raw_payload->>'execution_attempt_id',
    raw_payload->'order_state'->>'execution_attempt_id',
    raw_payload->'intent'->>'execution_attempt_id',
    raw_payload->'order_state'->'submission_payload'->>'executionAttemptId',
    raw_payload->'intent'->'submission_payload'->>'executionAttemptId'
)
WHERE execution_attempt_id IS NULL;

UPDATE execution_fills
SET execution_attempt_id = COALESCE(
    execution_attempt_id,
    raw_payload->>'execution_attempt_id',
    raw_payload->'fill_event'->>'execution_attempt_id',
    raw_payload->'fill_event'->'submission_payload'->>'executionAttemptId'
)
WHERE execution_attempt_id IS NULL;

UPDATE fill_outcomes
SET execution_attempt_id = COALESCE(
    execution_attempt_id,
    payload->>'execution_attempt_id'
)
WHERE execution_attempt_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_execution_orders_execution_attempt_id
    ON execution_orders (execution_attempt_id);

CREATE INDEX IF NOT EXISTS ix_execution_fills_execution_attempt_id
    ON execution_fills (execution_attempt_id);

CREATE INDEX IF NOT EXISTS ix_fill_outcomes_execution_attempt_id
    ON fill_outcomes (execution_attempt_id);
