-- Migration 0010: Additional indexes and constraints
-- Phase 1: Research Data Platform

-- Trigger function to auto-update updated_at on row modification
CREATE OR REPLACE FUNCTION meta.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply updated_at triggers to all meta tables
DO $body$
DECLARE
    tbl TEXT;
    meta_tbls TEXT[] := ARRAY[
        'meta.dataset_manifests',
        'meta.raw_source_files',
        'meta.ingest_runs',
        'meta.ingest_run_items',
        'meta.ingest_checkpoints',
        'meta.quality_reports'
    ];
BEGIN
    FOREACH tbl IN ARRAY meta_tbls LOOP
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER trg_%s_updated_at
             BEFORE UPDATE ON %s
             FOR EACH ROW EXECUTE FUNCTION meta.set_updated_at()',
            replace(replace(tbl, '.', '_'), 'meta_', ''),
            tbl
        );
    END LOOP;
END
$body$;

-- Apply updated_at triggers to all fact tables
DO $body$
DECLARE
    tbl TEXT;
    fact_tbls TEXT[] := ARRAY[
        'staging.market_swap_funding',
        'bronze.market_swap_funding',
        'silver.market_swap_funding'
    ];
    candle_schemas TEXT[] := ARRAY['staging', 'bronze', 'silver'];
    candle_types TEXT[] := ARRAY['spot', 'swap'];
    tfs TEXT[] := ARRAY['1m', '5m', '15m', '1h'];
    s TEXT; ct TEXT; tf TEXT;
BEGIN
    -- Candle tables
    FOREACH s IN ARRAY candle_schemas LOOP
        FOREACH ct IN ARRAY candle_types LOOP
            FOREACH tf IN ARRAY tfs LOOP
                tbl := format('%s.market_%s_candles_%s', s, ct, tf);
                EXECUTE format(
                    'CREATE OR REPLACE TRIGGER trg_%s_updated_at
                     BEFORE UPDATE ON %s
                     FOR EACH ROW EXECUTE FUNCTION meta.set_updated_at()',
                    replace(tbl, '.', '_'), tbl
                );
            END LOOP;
        END LOOP;
    END LOOP;
    -- Funding tables
    FOREACH tbl IN ARRAY fact_tbls LOOP
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER trg_%s_updated_at
             BEFORE UPDATE ON %s
             FOR EACH ROW EXECUTE FUNCTION meta.set_updated_at()',
            replace(tbl, '.', '_'), tbl
        );
    END LOOP;
    -- Gold replay bar tables
    FOREACH ct IN ARRAY ARRAY['spot', 'swap'] LOOP
        FOREACH tf IN ARRAY tfs LOOP
            tbl := format('gold.market_%s_replay_bars_%s', ct, tf);
            EXECUTE format(
                'CREATE OR REPLACE TRIGGER trg_%s_updated_at
                 BEFORE UPDATE ON %s
                 FOR EACH ROW EXECUTE FUNCTION meta.set_updated_at()',
                replace(tbl, '.', '_'), tbl
            );
        END LOOP;
    END LOOP;
END
$body$;
