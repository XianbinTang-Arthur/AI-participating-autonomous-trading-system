-- Batch B · Stage 03 — Phase 2 cost calibration
-- 回滚: batch_b_03_rollback.sql

BEGIN;

CREATE TABLE IF NOT EXISTS governance.cost_calibration_runs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(128) NOT NULL UNIQUE,
    symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) NOT NULL,
    observation_days INTEGER NOT NULL DEFAULT 7,
    fills_count INTEGER NOT NULL,
    effective_taker_fee_bps NUMERIC(8, 3) NOT NULL,
    effective_slippage_bps NUMERIC(8, 3) NOT NULL,
    current_taker_fee_bps NUMERIC(8, 3) NOT NULL,
    current_slippage_bps NUMERIC(8, 3) NOT NULL,
    drift_bps NUMERIC(8, 3) NOT NULL,
    recommendation_id VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_cost_calib_sym_tf
    ON governance.cost_calibration_runs(symbol, timeframe, created_at DESC);

COMMIT;
