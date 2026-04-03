-- Migration 0003: Create staging candle tables
-- Phase 1: Research Data Platform
-- 8 tables: spot/swap x 1m/5m/15m/1h

DO $body$
DECLARE
    tbl TEXT;
    tbls TEXT[] := ARRAY[
        'staging.market_spot_candles_1m',
        'staging.market_spot_candles_5m',
        'staging.market_spot_candles_15m',
        'staging.market_spot_candles_1h',
        'staging.market_swap_candles_1m',
        'staging.market_swap_candles_5m',
        'staging.market_swap_candles_15m',
        'staging.market_swap_candles_1h'
    ];
BEGIN
    FOREACH tbl IN ARRAY tbls LOOP
        EXECUTE format('
            CREATE TABLE IF NOT EXISTS %s (
                staging_row_id  BIGSERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL,
                ts              TIMESTAMPTZ NOT NULL,
                open            NUMERIC(20,10) NOT NULL,
                high            NUMERIC(20,10) NOT NULL,
                low             NUMERIC(20,10) NOT NULL,
                close           NUMERIC(20,10) NOT NULL,
                vol             NUMERIC(28,10) NULL,
                vol_ccy         NUMERIC(28,10) NULL,
                vol_quote       NUMERIC(28,10) NULL,
                confirm         BOOLEAN NOT NULL DEFAULT TRUE,
                raw_symbol      TEXT NULL,
                raw_ts          TEXT NULL,
                source_file_id  UUID NULL,
                ingest_run_id   UUID NOT NULL,
                dataset_version TEXT NOT NULL,
                quality_flags   TEXT[] NOT NULL DEFAULT ''{}'',
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            )', tbl);

        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (symbol, ts)',
            'idx_' || replace(replace(tbl, '.', '_'), 'staging_', 'stg_') || '_sym_ts', tbl);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (ingest_run_id)',
            'idx_' || replace(replace(tbl, '.', '_'), 'staging_', 'stg_') || '_run', tbl);
    END LOOP;
END
$body$;
