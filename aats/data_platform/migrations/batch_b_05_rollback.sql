-- Batch B · Stage 05 rollback — P1-D Phase 1A microstructure 表清理
--
-- 逆序 drop 4 张表; 由于 4 张表互相没有 FK 依赖(各自独立),drop 顺序
-- 其实没强制依赖,但仍按 stage 01 范式"后建先删"写:
--   staging.market_oi_funding_ticks → bronze.market_orderbook_books5
--   → bronze.market_orderbook_bbo   → bronze.market_trades
--
-- 索引随表 drop 自动清理; CHECK / PK 同理。
-- schema 本身不 drop (其他 migration 可能也在用同一 schema)。

BEGIN;

DROP TABLE IF EXISTS staging.market_oi_funding_ticks;
DROP TABLE IF EXISTS bronze.market_orderbook_books5;
DROP TABLE IF EXISTS bronze.market_orderbook_bbo;
DROP TABLE IF EXISTS bronze.market_trades;

COMMIT;
