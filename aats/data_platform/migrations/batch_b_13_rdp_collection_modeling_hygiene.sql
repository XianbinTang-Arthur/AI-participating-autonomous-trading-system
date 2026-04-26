-- Batch B · Stage 13 — RDP collection modeling hygiene
--
-- Purpose:
--   Close the gaps exposed by the aats_research audit without changing live
--   trading behavior:
--     - make microstructure a first-class metadata domain
--     - label liquidation raw rows by source scope
--     - enforce future orderbook payload sidecar lineage
--     - document dormant/unwired collection surfaces in DB metadata
--
-- Boundaries:
--   - schema/modeling/governance only
--   - no strategy, risk, execution, provider, symbol, venue, release,
--     promotion, tuning, or live order behavior change
--
-- Rollback: batch_b_13_rdp_collection_modeling_hygiene_rollback.sql.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────
-- 1. Metadata constraints: microstructure is already a production domain
--    for ingest_runs/checkpoints/run_items. Bring the remaining metadata
--    tables in line so manifests/source-files/quality reports can represent
--    the same domain without ad-hoc exceptions.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE meta.dataset_manifests DROP CONSTRAINT IF EXISTS chk_dm_domain;
ALTER TABLE meta.dataset_manifests ADD CONSTRAINT chk_dm_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text,
        'microstructure'::text
    ]));

ALTER TABLE meta.dataset_manifests DROP CONSTRAINT IF EXISTS chk_dm_source;
ALTER TABLE meta.dataset_manifests ADD CONSTRAINT chk_dm_source
    CHECK (source_type = ANY (ARRAY[
        'historical_file'::text,
        'api'::text,
        'api_stream'::text,
        'derived'::text
    ]));

ALTER TABLE meta.dataset_manifests DROP CONSTRAINT IF EXISTS chk_dm_status;
ALTER TABLE meta.dataset_manifests ADD CONSTRAINT chk_dm_status
    CHECK (status = ANY (ARRAY[
        'active'::text,
        'superseded'::text,
        'building'::text,
        'failed'::text,
        'dormant'::text
    ]));

ALTER TABLE meta.raw_source_files DROP CONSTRAINT IF EXISTS chk_rsf_domain;
ALTER TABLE meta.raw_source_files ADD CONSTRAINT chk_rsf_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text,
        'microstructure'::text
    ]));

ALTER TABLE meta.raw_source_files DROP CONSTRAINT IF EXISTS chk_rsf_source;
ALTER TABLE meta.raw_source_files ADD CONSTRAINT chk_rsf_source
    CHECK (source_type = ANY (ARRAY[
        'historical_file'::text,
        'api_snapshot'::text,
        'api_stream'::text
    ]));

ALTER TABLE meta.quality_reports DROP CONSTRAINT IF EXISTS chk_qr_domain;
ALTER TABLE meta.quality_reports ADD CONSTRAINT chk_qr_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text,
        'microstructure'::text
    ]));

-- ─────────────────────────────────────────────────────────────────────
-- 2. Liquidation raw stream scope marker.
--    OKX liquidation-orders is broad SWAP market context. Rows for the fixed
--    trading instrument are still explicitly labeled so audits do not confuse
--    broad collection with symbol-scope drift.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE staging.raw_liquidations
    ADD COLUMN IF NOT EXISTS source_scope TEXT NOT NULL DEFAULT 'broad_market_context';

UPDATE staging.raw_liquidations
SET source_scope = CASE
    WHEN inst_id = 'BTC-USDT-SWAP' THEN 'fixed_trading_scope'
    ELSE 'broad_market_context'
END
WHERE source_scope IS DISTINCT FROM CASE
    WHEN inst_id = 'BTC-USDT-SWAP' THEN 'fixed_trading_scope'
    ELSE 'broad_market_context'
END;

ALTER TABLE staging.raw_liquidations DROP CONSTRAINT IF EXISTS chk_raw_liq_source_scope;
ALTER TABLE staging.raw_liquidations ADD CONSTRAINT chk_raw_liq_source_scope
    CHECK (source_scope = ANY (ARRAY[
        'fixed_trading_scope'::text,
        'broad_market_context'::text
    ]));

CREATE INDEX IF NOT EXISTS ix_raw_liquidations_scope_inst_ts
    ON staging.raw_liquidations (source_scope, inst_id, ts);

COMMENT ON COLUMN staging.raw_liquidations.source_scope IS
    'fixed_trading_scope for BTC-USDT-SWAP rows; broad_market_context for wider OKX SWAP liquidation tape.';

-- ─────────────────────────────────────────────────────────────────────
-- 3. Future sidecar writes must point at a known ingest_run. NOT VALID keeps
--    historical deployment cheap; PostgreSQL still enforces new writes.
-- ─────────────────────────────────────────────────────────────────────

DO $$
BEGIN
    IF to_regclass('bronze.market_orderbook_payloads') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM pg_constraint
           WHERE conname = 'fk_brz_orderbook_payloads_ingest_run'
       ) THEN
        ALTER TABLE bronze.market_orderbook_payloads
            ADD CONSTRAINT fk_brz_orderbook_payloads_ingest_run
            FOREIGN KEY (ingest_run_id)
            REFERENCES meta.ingest_runs(ingest_run_id)
            NOT VALID;
    END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────
-- 4. Comments are intentionally operational metadata: audits can now
--    distinguish expected dormant surfaces from broken active collection.
-- ─────────────────────────────────────────────────────────────────────

COMMENT ON TABLE bronze.market_orderbook_payloads IS
    'Orderbook payload sidecar for execution science. Schema and lineage constraint are present; collector writes are not connected until the runtime-affecting payload-write task.';

COMMENT ON TABLE staging.raw_liquidations IS
    'OKX SWAP liquidation-orders raw stream. Broad-market context collection is intentional; source_scope marks BTC-USDT-SWAP fixed-scope rows.';

DO $$
DECLARE
    table_name TEXT;
    comment_text TEXT := 'Dormant by current RDP contract: table exists for future/replay compatibility but is not an active live collection surface.';
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'staging.market_spot_candles_1m',
        'staging.market_spot_candles_5m',
        'staging.market_swap_candles_1m',
        'staging.market_swap_candles_5m',
        'bronze.market_spot_candles_1m',
        'bronze.market_spot_candles_5m',
        'bronze.market_swap_candles_1m',
        'bronze.market_swap_candles_5m',
        'silver.market_spot_candles_1m',
        'silver.market_spot_candles_5m',
        'silver.market_swap_candles_1m',
        'silver.market_swap_candles_5m',
        'gold.market_spot_replay_bars_1m',
        'gold.market_spot_replay_bars_5m',
        'gold.market_spot_replay_bars_15m',
        'gold.market_spot_replay_bars_1h',
        'gold.market_swap_replay_bars_1m',
        'gold.market_swap_replay_bars_5m'
    ]
    LOOP
        IF to_regclass(table_name) IS NOT NULL THEN
            EXECUTE format('COMMENT ON TABLE %s IS %L', table_name, comment_text);
        END IF;
    END LOOP;
END $$;

COMMIT;
