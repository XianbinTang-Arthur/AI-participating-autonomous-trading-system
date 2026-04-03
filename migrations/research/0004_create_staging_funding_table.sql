-- Migration 0004: Create staging funding table
-- Phase 1: Research Data Platform

CREATE TABLE IF NOT EXISTS staging.market_swap_funding (
    staging_row_id  BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    funding_rate    NUMERIC(18,12) NOT NULL,
    inst_type       TEXT NULL,
    formula_type    TEXT NULL,
    method          TEXT NULL,
    realized_rate   NUMERIC(18,12) NULL,
    raw_symbol      TEXT NULL,
    raw_ts          TEXT NULL,
    source_file_id  UUID NULL,
    ingest_run_id   UUID NOT NULL,
    dataset_version TEXT NOT NULL,
    quality_flags   TEXT[] NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stg_swap_funding_sym_ts ON staging.market_swap_funding (symbol, ts);
CREATE INDEX IF NOT EXISTS idx_stg_swap_funding_run    ON staging.market_swap_funding (ingest_run_id);
