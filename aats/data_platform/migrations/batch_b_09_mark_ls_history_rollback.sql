-- Batch B · Stage 09 rollback — mark-price + long-short history bronze 表清理
--
-- 逆序 drop 2 张表. 索引随表 drop 自动清理; PK 同理.
-- schema 本身不 drop (其他 migration 共用 bronze schema).

BEGIN;

DROP TABLE IF EXISTS bronze.market_long_short_ratio_5m;
DROP TABLE IF EXISTS bronze.market_mark_price_candles_1m;

COMMIT;
