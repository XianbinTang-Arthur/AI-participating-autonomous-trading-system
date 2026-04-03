-- Migration 0008: Create silver funding table
-- Phase 1: Research Data Platform

CREATE TABLE IF NOT EXISTS silver.market_swap_funding (
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
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts)
);

CREATE INDEX IF NOT EXISTS idx_slv_swap_funding_ts  ON silver.market_swap_funding (ts);
CREATE INDEX IF NOT EXISTS idx_slv_swap_funding_run ON silver.market_swap_funding (ingest_run_id);
CREATE INDEX IF NOT EXISTS idx_slv_swap_funding_ver ON silver.market_swap_funding (dataset_version);
