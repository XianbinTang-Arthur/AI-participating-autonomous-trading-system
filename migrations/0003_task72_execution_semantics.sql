ALTER TABLE order_states
    ADD COLUMN IF NOT EXISTS reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS close_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS td_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS position_mode VARCHAR(32),
    ADD COLUMN IF NOT EXISTS pos_side VARCHAR(16),
    ADD COLUMN IF NOT EXISTS reduce_only_reason VARCHAR(64),
    ADD COLUMN IF NOT EXISTS close_only_reason VARCHAR(64),
    ADD COLUMN IF NOT EXISTS instrument_family VARCHAR(64),
    ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(16);

UPDATE order_states
SET
    reduce_only = COALESCE(
        reduce_only,
        LOWER(COALESCE(payload->>'reduce_only', payload->'submission_payload'->>'reduceOnly', 'false')) = 'true'
    ),
    close_only = COALESCE(
        close_only,
        LOWER(COALESCE(payload->>'close_only', payload->'submission_payload'->>'closeOnly', 'false')) = 'true'
    ),
    td_mode = COALESCE(
        NULLIF(payload->>'td_mode', ''),
        NULLIF(payload->'submission_payload'->>'tdMode', ''),
        td_mode
    ),
    position_mode = COALESCE(
        NULLIF(payload->>'position_mode', ''),
        NULLIF(payload->'submission_payload'->>'positionMode', ''),
        position_mode
    ),
    pos_side = COALESCE(
        NULLIF(payload->>'pos_side', ''),
        NULLIF(payload->'submission_payload'->>'posSide', ''),
        pos_side
    ),
    reduce_only_reason = COALESCE(
        NULLIF(payload->>'reduce_only_reason', ''),
        NULLIF(payload->'submission_payload'->>'reduceOnlyReason', ''),
        reduce_only_reason
    ),
    close_only_reason = COALESCE(
        NULLIF(payload->>'close_only_reason', ''),
        NULLIF(payload->'submission_payload'->>'closeOnlyReason', ''),
        close_only_reason
    ),
    instrument_family = COALESCE(
        NULLIF(payload->>'instrument_family', ''),
        NULLIF(payload->'submission_payload'->>'instrumentFamily', ''),
        instrument_family
    ),
    settle_currency = COALESCE(
        NULLIF(payload->>'settle_currency', ''),
        NULLIF(payload->'submission_payload'->>'settleCurrency', ''),
        settle_currency
    )
WHERE
    td_mode IS NULL
    OR position_mode IS NULL
    OR pos_side IS NULL
    OR instrument_family IS NULL
    OR settle_currency IS NULL;

ALTER TABLE fill_events
    ADD COLUMN IF NOT EXISTS reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS close_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS td_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS position_mode VARCHAR(32),
    ADD COLUMN IF NOT EXISTS pos_side VARCHAR(16),
    ADD COLUMN IF NOT EXISTS reduce_only_reason VARCHAR(64),
    ADD COLUMN IF NOT EXISTS close_only_reason VARCHAR(64),
    ADD COLUMN IF NOT EXISTS instrument_family VARCHAR(64),
    ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(16);

UPDATE fill_events
SET
    reduce_only = COALESCE(
        reduce_only,
        LOWER(COALESCE(payload->>'reduce_only', 'false')) = 'true'
    ),
    close_only = COALESCE(
        close_only,
        LOWER(COALESCE(payload->>'close_only', 'false')) = 'true'
    ),
    td_mode = COALESCE(NULLIF(payload->>'td_mode', ''), td_mode),
    position_mode = COALESCE(NULLIF(payload->>'position_mode', ''), position_mode),
    pos_side = COALESCE(NULLIF(payload->>'pos_side', ''), pos_side),
    reduce_only_reason = COALESCE(NULLIF(payload->>'reduce_only_reason', ''), reduce_only_reason),
    close_only_reason = COALESCE(NULLIF(payload->>'close_only_reason', ''), close_only_reason),
    instrument_family = COALESCE(NULLIF(payload->>'instrument_family', ''), instrument_family),
    settle_currency = COALESCE(NULLIF(payload->>'settle_currency', ''), settle_currency)
WHERE
    td_mode IS NULL
    OR position_mode IS NULL
    OR pos_side IS NULL
    OR instrument_family IS NULL
    OR settle_currency IS NULL;

ALTER TABLE execution_orders
    ADD COLUMN IF NOT EXISTS reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS close_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS td_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS position_mode VARCHAR(32),
    ADD COLUMN IF NOT EXISTS pos_side VARCHAR(16),
    ADD COLUMN IF NOT EXISTS reduce_only_reason VARCHAR(64),
    ADD COLUMN IF NOT EXISTS close_only_reason VARCHAR(64),
    ADD COLUMN IF NOT EXISTS instrument_family VARCHAR(64),
    ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(16);

UPDATE execution_orders
SET
    reduce_only = COALESCE(
        reduce_only,
        LOWER(
            COALESCE(
                raw_payload->'order_state'->>'reduce_only',
                raw_payload->'fill_event'->>'reduce_only',
                'false'
            )
        ) = 'true'
    ),
    close_only = COALESCE(
        close_only,
        LOWER(
            COALESCE(
                raw_payload->'order_state'->>'close_only',
                raw_payload->'fill_event'->>'close_only',
                'false'
            )
        ) = 'true'
    ),
    td_mode = COALESCE(
        NULLIF(raw_payload->'order_state'->>'td_mode', ''),
        NULLIF(raw_payload->'order_state'->'submission_payload'->>'tdMode', ''),
        NULLIF(raw_payload->'fill_event'->>'td_mode', ''),
        td_mode
    ),
    position_mode = COALESCE(
        NULLIF(raw_payload->'order_state'->>'position_mode', ''),
        NULLIF(raw_payload->'order_state'->'submission_payload'->>'positionMode', ''),
        NULLIF(raw_payload->'fill_event'->>'position_mode', ''),
        position_mode
    ),
    pos_side = COALESCE(
        NULLIF(raw_payload->'order_state'->>'pos_side', ''),
        NULLIF(raw_payload->'order_state'->'submission_payload'->>'posSide', ''),
        NULLIF(raw_payload->'fill_event'->>'pos_side', ''),
        pos_side
    ),
    reduce_only_reason = COALESCE(
        NULLIF(raw_payload->'order_state'->>'reduce_only_reason', ''),
        NULLIF(raw_payload->'order_state'->'submission_payload'->>'reduceOnlyReason', ''),
        NULLIF(raw_payload->'fill_event'->>'reduce_only_reason', ''),
        reduce_only_reason
    ),
    close_only_reason = COALESCE(
        NULLIF(raw_payload->'order_state'->>'close_only_reason', ''),
        NULLIF(raw_payload->'order_state'->'submission_payload'->>'closeOnlyReason', ''),
        NULLIF(raw_payload->'fill_event'->>'close_only_reason', ''),
        close_only_reason
    ),
    instrument_family = COALESCE(
        NULLIF(raw_payload->'order_state'->>'instrument_family', ''),
        NULLIF(raw_payload->'order_state'->'submission_payload'->>'instrumentFamily', ''),
        NULLIF(raw_payload->'fill_event'->>'instrument_family', ''),
        instrument_family
    ),
    settle_currency = COALESCE(
        NULLIF(raw_payload->'order_state'->>'settle_currency', ''),
        NULLIF(raw_payload->'order_state'->'submission_payload'->>'settleCurrency', ''),
        NULLIF(raw_payload->'fill_event'->>'settle_currency', ''),
        settle_currency
    )
WHERE
    td_mode IS NULL
    OR position_mode IS NULL
    OR pos_side IS NULL
    OR instrument_family IS NULL
    OR settle_currency IS NULL;

ALTER TABLE execution_fills
    ADD COLUMN IF NOT EXISTS reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS close_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS td_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS position_mode VARCHAR(32),
    ADD COLUMN IF NOT EXISTS pos_side VARCHAR(16),
    ADD COLUMN IF NOT EXISTS reduce_only_reason VARCHAR(64),
    ADD COLUMN IF NOT EXISTS close_only_reason VARCHAR(64),
    ADD COLUMN IF NOT EXISTS instrument_family VARCHAR(64),
    ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(16);

UPDATE execution_fills
SET
    reduce_only = COALESCE(
        reduce_only,
        LOWER(COALESCE(raw_payload->'fill_event'->>'reduce_only', 'false')) = 'true'
    ),
    close_only = COALESCE(
        close_only,
        LOWER(COALESCE(raw_payload->'fill_event'->>'close_only', 'false')) = 'true'
    ),
    td_mode = COALESCE(NULLIF(raw_payload->'fill_event'->>'td_mode', ''), td_mode),
    position_mode = COALESCE(NULLIF(raw_payload->'fill_event'->>'position_mode', ''), position_mode),
    pos_side = COALESCE(NULLIF(raw_payload->'fill_event'->>'pos_side', ''), pos_side),
    reduce_only_reason = COALESCE(NULLIF(raw_payload->'fill_event'->>'reduce_only_reason', ''), reduce_only_reason),
    close_only_reason = COALESCE(NULLIF(raw_payload->'fill_event'->>'close_only_reason', ''), close_only_reason),
    instrument_family = COALESCE(NULLIF(raw_payload->'fill_event'->>'instrument_family', ''), instrument_family),
    settle_currency = COALESCE(NULLIF(raw_payload->'fill_event'->>'settle_currency', ''), settle_currency)
WHERE
    td_mode IS NULL
    OR position_mode IS NULL
    OR pos_side IS NULL
    OR instrument_family IS NULL
    OR settle_currency IS NULL;
