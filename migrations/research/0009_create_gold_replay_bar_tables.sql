-- Migration 0009: Create gold replay bar tables
-- Phase 1: Research Data Platform
-- 8 tables: spot/swap x 1m/5m/15m/1h

DO $body$
DECLARE
    tbl TEXT;
    tbls TEXT[] := ARRAY[
        'gold.market_spot_replay_bars_1m',
        'gold.market_spot_replay_bars_5m',
        'gold.market_spot_replay_bars_15m',
        'gold.market_spot_replay_bars_1h',
        'gold.market_swap_replay_bars_1m',
        'gold.market_swap_replay_bars_5m',
        'gold.market_swap_replay_bars_15m',
        'gold.market_swap_replay_bars_1h'
    ];
BEGIN
    FOREACH tbl IN ARRAY tbls LOOP
        EXECUTE format('
            CREATE TABLE IF NOT EXISTS %s (
                symbol                          TEXT NOT NULL,
                ts                              TIMESTAMPTZ NOT NULL,
                open                            NUMERIC(20,10) NOT NULL,
                high                            NUMERIC(20,10) NOT NULL,
                low                             NUMERIC(20,10) NOT NULL,
                close                           NUMERIC(20,10) NOT NULL,
                volume                          NUMERIC(28,10) NULL,
                quote_volume                    NUMERIC(28,10) NULL,
                is_closed                       BOOLEAN NOT NULL DEFAULT TRUE,
                aligned_funding_rate            NUMERIC(18,12) NULL,
                funding_source_ts               TIMESTAMPTZ NULL,
                source_candle_dataset_version   TEXT NOT NULL,
                source_funding_dataset_version  TEXT NULL,
                build_run_id                    UUID NOT NULL,
                quality_flags                   TEXT[] NOT NULL DEFAULT ''{}'',
                created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, ts)
            )', tbl);

        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (ts)',
            'idx_' || replace(replace(tbl, '.', '_'), 'gold_', 'gld_') || '_ts', tbl);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (build_run_id)',
            'idx_' || replace(replace(tbl, '.', '_'), 'gold_', 'gld_') || '_bld', tbl);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (source_candle_dataset_version)',
            'idx_' || replace(replace(tbl, '.', '_'), 'gold_', 'gld_') || '_ver', tbl);
    END LOOP;
END
$body$;
