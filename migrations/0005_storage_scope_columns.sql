ALTER TABLE event_store
    ADD COLUMN IF NOT EXISTS symbol VARCHAR(64),
    ADD COLUMN IF NOT EXISTS timeframe VARCHAR(16),
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);

UPDATE event_store
SET
    symbol = COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0),
    timeframe = NULLIF(payload->>'timeframe', ''),
    product_type = COALESCE(NULLIF(payload->>'product_type', ''), CASE
        WHEN COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0) LIKE '%-SWAP' THEN 'derivatives'
        ELSE NULL
    END),
    margin_mode = COALESCE(NULLIF(payload->>'margin_mode', ''), CASE
        WHEN COALESCE(NULLIF(payload->>'product_type', ''), CASE
            WHEN COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0) LIKE '%-SWAP' THEN 'derivatives'
            ELSE 'spot'
        END) = 'spot' THEN 'cash'
        ELSE NULL
    END)
WHERE symbol IS NULL
   OR timeframe IS NULL
   OR product_type IS NULL
   OR margin_mode IS NULL;

CREATE INDEX IF NOT EXISTS ix_event_store_topic_symbol_seq
    ON event_store (topic, symbol, sequence_id);
CREATE INDEX IF NOT EXISTS ix_event_store_topic_scope_seq
    ON event_store (topic, product_type, margin_mode, sequence_id);

ALTER TABLE portfolio_snapshots
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS primary_symbol VARCHAR(64);

UPDATE portfolio_snapshots
SET
    product_type = COALESCE(NULLIF(payload->>'product_type', ''), 'spot'),
    margin_mode = COALESCE(NULLIF(payload->>'margin_mode', ''), 'cash'),
    primary_symbol = NULLIF(payload->'positions'->0->>'symbol', '')
WHERE product_type IS NULL
   OR margin_mode IS NULL
   OR primary_symbol IS NULL;

CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_scope_seq
    ON portfolio_snapshots (product_type, margin_mode, sequence_id);

ALTER TABLE order_states
    ADD COLUMN IF NOT EXISTS symbol VARCHAR(32),
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS position_intent VARCHAR(32);

UPDATE order_states
SET
    symbol = COALESCE(NULLIF(payload->>'symbol', ''), symbol),
    product_type = COALESCE(
        NULLIF(payload->>'product_type', ''),
        CASE WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END
    ),
    margin_mode = COALESCE(
        NULLIF(payload->>'margin_mode', ''),
        NULLIF(payload->'submission_payload'->>'tdMode', ''),
        CASE
            WHEN COALESCE(NULLIF(payload->>'product_type', ''), CASE WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END) = 'spot'
                THEN 'cash'
            ELSE 'cross'
        END
    ),
    position_intent = NULLIF(payload->>'position_intent', '')
WHERE symbol IS NULL
   OR product_type IS NULL
   OR margin_mode IS NULL
   OR position_intent IS NULL;

CREATE INDEX IF NOT EXISTS ix_order_states_scope_status
    ON order_states (product_type, margin_mode, status);
CREATE INDEX IF NOT EXISTS ix_order_states_scope_symbol_update
    ON order_states (product_type, margin_mode, symbol, last_update_ts);

ALTER TABLE fill_events
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS position_intent VARCHAR(32);

UPDATE fill_events
SET
    product_type = COALESCE(
        NULLIF(payload->>'product_type', ''),
        CASE WHEN symbol LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END
    ),
    margin_mode = COALESCE(
        NULLIF(payload->>'margin_mode', ''),
        CASE WHEN COALESCE(NULLIF(payload->>'product_type', ''), CASE WHEN symbol LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END) = 'spot'
            THEN 'cash'
            ELSE 'cross'
        END
    ),
    position_intent = NULLIF(payload->>'position_intent', '')
WHERE product_type IS NULL
   OR margin_mode IS NULL
   OR position_intent IS NULL;

CREATE INDEX IF NOT EXISTS ix_fill_events_scope_symbol_ts
    ON fill_events (product_type, margin_mode, symbol, ingestion_timestamp);

ALTER TABLE reconciliation_reports
    ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS primary_symbol VARCHAR(64);

UPDATE reconciliation_reports
SET
    decision_id = NULLIF(payload->>'decision_id', ''),
    product_type = NULLIF(payload->>'product_type', ''),
    margin_mode = NULLIF(payload->>'margin_mode', ''),
    primary_symbol = COALESCE(NULLIF(payload->'allowed_symbols'->>0, ''), primary_symbol)
WHERE decision_id IS NULL
   OR product_type IS NULL
   OR margin_mode IS NULL
   OR primary_symbol IS NULL;

CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_scope_ts
    ON reconciliation_reports (product_type, margin_mode, as_of_ts);
