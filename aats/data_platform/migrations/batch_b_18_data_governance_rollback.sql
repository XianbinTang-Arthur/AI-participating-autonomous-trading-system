-- Destructive rollback. Stop all new writers first; archived/raw facts are external
-- to these ledgers and must not be deleted as part of the rollback.

BEGIN;

DROP TABLE IF EXISTS silver.historical_trade_flow_15m;
DROP TABLE IF EXISTS silver.historical_orderbook_metrics_15m;
DROP TABLE IF EXISTS bronze.market_mark_price_candles_1h;
DROP TABLE IF EXISTS bronze.market_mark_price_candles_15m;
DROP TABLE IF EXISTS bronze.historical_orderbook_books5_2hz;
DROP TABLE IF EXISTS bronze.historical_orderbook_bbo_1hz;
DROP TABLE IF EXISTS staging.official_l2_history;
DROP TABLE IF EXISTS staging.official_trade_history;

ALTER TABLE staging.raw_liquidations
    DROP CONSTRAINT IF EXISTS chk_raw_liq_payload_hash;
ALTER TABLE staging.raw_liquidations
    DROP COLUMN IF EXISTS raw_payload_hash;

DROP TABLE IF EXISTS meta.collector_continuity_events;
DROP TABLE IF EXISTS meta.data_rebuild_runs;
DROP TABLE IF EXISTS meta.dataset_bundles;
DROP TABLE IF EXISTS meta.data_gap_records;
DROP TABLE IF EXISTS meta.archive_partitions;
DROP TABLE IF EXISTS meta.data_source_registry;

COMMIT;
