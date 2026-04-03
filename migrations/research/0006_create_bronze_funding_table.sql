-- Migration 0006: Create bronze funding table
-- Phase 1: Research Data Platform

CREATE TABLE IF NOT EXISTS bronze.market_swap_funding (
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

CREATE INDEX IF NOT EXISTS idx_brz_swap_funding_ts  ON bronze.market_swap_funding (ts);
CREATE INDEX IF NOT EXISTS idx_brz_swap_funding_run ON bronze.market_swap_funding (ingest_run_id);
