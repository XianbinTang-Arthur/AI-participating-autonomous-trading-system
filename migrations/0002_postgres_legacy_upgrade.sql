-- AATS PostgreSQL legacy upgrade normalization
-- Applies idempotent schema changes needed to upgrade historical production schemas
-- to the current latest baseline without requiring the removed incremental chain.

ALTER TABLE event_store
    ADD COLUMN IF NOT EXISTS symbol VARCHAR(64),
    ADD COLUMN IF NOT EXISTS timeframe VARCHAR(16),
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);

UPDATE event_store
SET
    symbol = COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0, symbol),
    timeframe = COALESCE(NULLIF(payload->>'timeframe', ''), timeframe),
    product_type = COALESCE(
        NULLIF(payload->>'product_type', ''),
        CASE
            WHEN COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0, symbol) LIKE '%-SWAP' THEN 'derivatives'
            ELSE NULL
        END,
        product_type
    ),
    margin_mode = COALESCE(
        NULLIF(payload->>'margin_mode', ''),
        CASE
            WHEN COALESCE(
                NULLIF(payload->>'product_type', ''),
                CASE
                    WHEN COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0, symbol) LIKE '%-SWAP' THEN 'derivatives'
                    ELSE 'spot'
                END
            ) = 'spot'
                THEN 'cash'
            ELSE 'cross'
        END,
        margin_mode
    )
WHERE symbol IS NULL
   OR timeframe IS NULL
   OR product_type IS NULL
   OR margin_mode IS NULL;

CREATE INDEX IF NOT EXISTS ix_event_store_topic_symbol_seq ON event_store (topic, symbol, sequence_id);
CREATE INDEX IF NOT EXISTS ix_event_store_topic_scope_seq ON event_store (topic, product_type, margin_mode, sequence_id);
CREATE INDEX IF NOT EXISTS ix_event_store_symbol ON event_store (symbol);
CREATE INDEX IF NOT EXISTS ix_event_store_timeframe ON event_store (timeframe);
CREATE INDEX IF NOT EXISTS ix_event_store_product_type ON event_store (product_type);
CREATE INDEX IF NOT EXISTS ix_event_store_margin_mode ON event_store (margin_mode);

ALTER TABLE portfolio_snapshots
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS primary_symbol VARCHAR(64);

UPDATE portfolio_snapshots
SET
    product_type = COALESCE(NULLIF(payload->>'product_type', ''), product_type, 'spot'),
    margin_mode = COALESCE(NULLIF(payload->>'margin_mode', ''), margin_mode, 'cash'),
    primary_symbol = COALESCE(NULLIF(payload->'positions'->0->>'symbol', ''), primary_symbol)
WHERE product_type IS NULL
   OR margin_mode IS NULL
   OR primary_symbol IS NULL;

ALTER TABLE portfolio_snapshots
    ALTER COLUMN product_type SET NOT NULL,
    ALTER COLUMN margin_mode SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_scope_seq ON portfolio_snapshots (product_type, margin_mode, sequence_id);
CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_product_type ON portfolio_snapshots (product_type);
CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_margin_mode ON portfolio_snapshots (margin_mode);
CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_primary_symbol ON portfolio_snapshots (primary_symbol);

ALTER TABLE order_states
    ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS symbol VARCHAR(32),
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS position_intent VARCHAR(32);

UPDATE order_states
SET
    decision_id = COALESCE(NULLIF(payload->>'decision_id', ''), decision_id, 'legacy_unknown'),
    symbol = COALESCE(NULLIF(payload->>'symbol', ''), symbol, 'legacy_unknown'),
    product_type = COALESCE(
        NULLIF(payload->>'product_type', ''),
        CASE WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END,
        product_type
    ),
    margin_mode = COALESCE(
        NULLIF(payload->>'margin_mode', ''),
        NULLIF(payload->'submission_payload'->>'tdMode', ''),
        CASE
            WHEN COALESCE(
                NULLIF(payload->>'product_type', ''),
                CASE WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END
            ) = 'spot'
                THEN 'cash'
            ELSE 'cross'
        END,
        margin_mode
    ),
    position_intent = COALESCE(NULLIF(payload->>'position_intent', ''), position_intent)
WHERE decision_id IS NULL
   OR symbol IS NULL
   OR product_type IS NULL
   OR margin_mode IS NULL
   OR position_intent IS NULL;

ALTER TABLE order_states
    ALTER COLUMN decision_id SET NOT NULL,
    ALTER COLUMN symbol SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_order_states_decision_id ON order_states (decision_id);
CREATE INDEX IF NOT EXISTS ix_order_states_symbol ON order_states (symbol);
CREATE INDEX IF NOT EXISTS ix_order_states_product_type ON order_states (product_type);
CREATE INDEX IF NOT EXISTS ix_order_states_margin_mode ON order_states (margin_mode);
CREATE INDEX IF NOT EXISTS ix_order_states_position_intent ON order_states (position_intent);
CREATE INDEX IF NOT EXISTS ix_order_states_scope_status ON order_states (product_type, margin_mode, status);
CREATE INDEX IF NOT EXISTS ix_order_states_scope_symbol_update ON order_states (product_type, margin_mode, symbol, last_update_ts);

ALTER TABLE fill_events
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS position_intent VARCHAR(32);

UPDATE fill_events
SET
    product_type = COALESCE(
        NULLIF(payload->>'product_type', ''),
        CASE WHEN symbol LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END,
        product_type
    ),
    margin_mode = COALESCE(
        NULLIF(payload->>'margin_mode', ''),
        CASE
            WHEN COALESCE(NULLIF(payload->>'product_type', ''), CASE WHEN symbol LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END) = 'spot'
                THEN 'cash'
            ELSE 'cross'
        END,
        margin_mode
    ),
    position_intent = COALESCE(NULLIF(payload->>'position_intent', ''), position_intent)
WHERE product_type IS NULL
   OR margin_mode IS NULL
   OR position_intent IS NULL;

CREATE INDEX IF NOT EXISTS ix_fill_events_scope_symbol_ts ON fill_events (product_type, margin_mode, symbol, ingestion_timestamp);
CREATE INDEX IF NOT EXISTS ix_fill_events_product_type ON fill_events (product_type);
CREATE INDEX IF NOT EXISTS ix_fill_events_margin_mode ON fill_events (margin_mode);
CREATE INDEX IF NOT EXISTS ix_fill_events_position_intent ON fill_events (position_intent);

ALTER TABLE reconciliation_reports
    ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16),
    ADD COLUMN IF NOT EXISTS primary_symbol VARCHAR(64);

UPDATE reconciliation_reports
SET
    decision_id = COALESCE(NULLIF(payload->>'decision_id', ''), decision_id),
    product_type = COALESCE(NULLIF(payload->>'product_type', ''), product_type),
    margin_mode = COALESCE(NULLIF(payload->>'margin_mode', ''), margin_mode),
    primary_symbol = COALESCE(NULLIF(payload->'allowed_symbols'->>0, ''), primary_symbol)
WHERE decision_id IS NULL
   OR product_type IS NULL
   OR margin_mode IS NULL
   OR primary_symbol IS NULL;

CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_scope_ts ON reconciliation_reports (product_type, margin_mode, as_of_ts);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_decision_id ON reconciliation_reports (decision_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_product_type ON reconciliation_reports (product_type);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_margin_mode ON reconciliation_reports (margin_mode);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_primary_symbol ON reconciliation_reports (primary_symbol);

ALTER TABLE decision_audit_records
    ADD COLUMN IF NOT EXISTS execution_plan_ref VARCHAR(64),
    ADD COLUMN IF NOT EXISTS order_state_refs JSONB;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'decision_audit_records'
          AND column_name = 'order_state_refs'
          AND data_type = 'json'
    ) THEN
        ALTER TABLE decision_audit_records
            ALTER COLUMN order_state_refs
            TYPE JSONB
            USING COALESCE(order_state_refs::jsonb, '[]'::jsonb);
    END IF;
END $$;

UPDATE decision_audit_records
SET execution_plan_ref = COALESCE(NULLIF(payload->>'execution_plan_ref', ''), execution_plan_ref)
WHERE execution_plan_ref IS NULL;

UPDATE decision_audit_records
SET order_state_refs = COALESCE(order_state_refs, payload->'order_state_refs', '[]'::jsonb)
WHERE order_state_refs IS NULL;

ALTER TABLE order_obligations
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);

UPDATE order_obligations
SET
    product_type = COALESCE(
        NULLIF(payload->>'product_type', ''),
        CASE WHEN symbol LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END,
        product_type
    ),
    margin_mode = COALESCE(
        NULLIF(payload->>'margin_mode', ''),
        CASE
            WHEN COALESCE(NULLIF(payload->>'product_type', ''), CASE WHEN symbol LIKE '%-SWAP' THEN 'derivatives' ELSE 'spot' END) = 'spot'
                THEN 'cash'
            ELSE 'cross'
        END,
        margin_mode
    )
WHERE product_type IS NULL
   OR margin_mode IS NULL;

CREATE INDEX IF NOT EXISTS ix_order_obligations_scope_status ON order_obligations (product_type, margin_mode, status);
CREATE INDEX IF NOT EXISTS ix_order_obligations_currency_status ON order_obligations (reserve_currency, status);

ALTER TABLE fill_outcomes
    ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS intent_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS order_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS venue VARCHAR(16),
    ADD COLUMN IF NOT EXISTS side VARCHAR(8),
    ADD COLUMN IF NOT EXISTS fill_qty NUMERIC(36, 18),
    ADD COLUMN IF NOT EXISTS fill_price NUMERIC(36, 18),
    ADD COLUMN IF NOT EXISTS fill_notional NUMERIC(36, 18),
    ADD COLUMN IF NOT EXISTS fee_amount NUMERIC(36, 18),
    ADD COLUMN IF NOT EXISTS fee_currency VARCHAR(16),
    ADD COLUMN IF NOT EXISTS liquidity_role VARCHAR(16),
    ADD COLUMN IF NOT EXISTS exchange_timestamp TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ingestion_timestamp TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS order_status_after_fill VARCHAR(32),
    ADD COLUMN IF NOT EXISTS target_leverage DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS exposure_side VARCHAR(16),
    ADD COLUMN IF NOT EXISTS execution_action VARCHAR(16),
    ADD COLUMN IF NOT EXISTS position_intent VARCHAR(32),
    ADD COLUMN IF NOT EXISTS starting_position_qty NUMERIC(36, 18),
    ADD COLUMN IF NOT EXISTS starting_avg_entry_price NUMERIC(36, 18),
    ADD COLUMN IF NOT EXISTS ending_position_qty NUMERIC(36, 18),
    ADD COLUMN IF NOT EXISTS ending_avg_entry_price NUMERIC(36, 18),
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);

CREATE INDEX IF NOT EXISTS ix_fill_outcomes_scope_symbol_ts ON fill_outcomes (product_type, margin_mode, symbol, created_at);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_order_id ON fill_outcomes (order_id, created_at);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_product_type ON fill_outcomes (product_type);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_margin_mode ON fill_outcomes (margin_mode);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_position_intent ON fill_outcomes (position_intent);

ALTER TABLE reservations
    ALTER COLUMN state TYPE VARCHAR(32);

ALTER TABLE settlements
    ALTER COLUMN state TYPE VARCHAR(32);

ALTER TABLE lot_events
    ADD COLUMN IF NOT EXISTS product_type VARCHAR(16),
    ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);

UPDATE lot_events le
SET
    product_type = COALESCE(le.product_type, pl.product_type, 'spot'),
    margin_mode = COALESCE(le.margin_mode, pl.margin_mode, 'cash')
FROM position_lots pl
WHERE le.lot_id = pl.lot_id
  AND (le.product_type IS NULL OR le.margin_mode IS NULL);

UPDATE lot_events
SET
    product_type = COALESCE(product_type, 'spot'),
    margin_mode = COALESCE(margin_mode, 'cash')
WHERE product_type IS NULL
   OR margin_mode IS NULL;

ALTER TABLE lot_events
    ALTER COLUMN product_type SET NOT NULL,
    ALTER COLUMN margin_mode SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_lot_events_scope_symbol ON lot_events (product_type, margin_mode, symbol);
CREATE INDEX IF NOT EXISTS ix_lot_events_product_type ON lot_events (product_type);
CREATE INDEX IF NOT EXISTS ix_lot_events_margin_mode ON lot_events (margin_mode);

DO $$
DECLARE
    existing_constraint TEXT;
BEGIN
    SELECT tc.constraint_name
    INTO existing_constraint
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_schema = kcu.constraint_schema
     AND tc.constraint_name = kcu.constraint_name
    WHERE tc.table_schema = current_schema()
      AND tc.table_name = 'lot_events'
      AND tc.constraint_type = 'FOREIGN KEY'
      AND kcu.column_name = 'lot_id'
    LIMIT 1;

    IF existing_constraint IS NOT NULL THEN
        EXECUTE format('ALTER TABLE lot_events DROP CONSTRAINT %I', existing_constraint);
    END IF;

    ALTER TABLE lot_events
        ADD CONSTRAINT lot_events_lot_id_fkey
        FOREIGN KEY (lot_id) REFERENCES position_lots(lot_id) ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN
        NULL;
END $$;
