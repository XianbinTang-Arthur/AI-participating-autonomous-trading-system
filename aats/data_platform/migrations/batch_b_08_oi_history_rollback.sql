-- Batch B · Stage 08 rollback — OI history bronze 表清理
--
-- 逆序 drop 1 张表. 索引随表 drop 自动清理; PK 同理.
-- schema 本身不 drop (其他 migration 共用 bronze schema).

BEGIN;

DROP TABLE IF EXISTS bronze.market_oi_history_1h;

COMMIT;
