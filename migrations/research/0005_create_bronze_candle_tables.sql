-- Migration 0005: Create bronze candle tables
-- Phase 1: Research Data Platform
-- 8 tables: spot/swap x 1m/5m/15m/1h

DO $body$
DECLARE
    tbl TEXT;
    tbls TEXT[] := ARRAY[
        'bronze.market_spot_candles_1m',
        'bronze.market_spot_candles_5m',
        'bronze.market_spot_candles_15m',
        'bronze.market_spot_candles_1h',
        'bronze.market_swap_candles_1m',
        'bronze.market_swap_candles_5m',
        'bronze.market_swap_candles_15m',
        'bronze.market_swap_candles_1h'
    ];
BEGIN
    FOREACH tbl IN ARRAY tbls LOOP
        EXECUTE format('
            CREATE TABLE IF NOT EXISTS %s (
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
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, ts)
            )', tbl);

        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (ts)',
            'idx_' || replace(replace(tbl, '.', '_'), 'bronze_', 'brz_') || '_ts', tbl);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (source_file_id)',
            'idx_' || replace(replace(tbl, '.', '_'), 'bronze_', 'brz_') || '_sf', tbl);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (ingest_run_id)',
            'idx_' || replace(replace(tbl, '.', '_'), 'bronze_', 'brz_') || '_run', tbl);
    END LOOP;
END
$body$;
