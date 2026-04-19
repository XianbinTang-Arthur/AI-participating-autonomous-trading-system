-- Batch B · Stage 06 rollback — P1-D Phase 1A Silver microstructure 表清理
--
-- 逆序 drop 5 张 silver 15m 表; 5 张表互相没有 FK 依赖 (各自独立,
-- ETL 在应用层 cross-reference),drop 顺序其实没强制依赖,但仍按
-- stage 05 范式 "后建先删" 写 (建表顺序: orderbook → trade_flow →
-- oi_funding → volume_profile → liquidation)。
--
-- 索引随表 drop 自动清理; PK/CHECK 同理。
-- schema 本身不 drop (其他 migration 共用 silver schema)。

BEGIN;

DROP TABLE IF EXISTS silver.market_liquidation_metrics_15m;
DROP TABLE IF EXISTS silver.market_volume_profile_15m;
DROP TABLE IF EXISTS silver.market_oi_funding_metrics_15m;
DROP TABLE IF EXISTS silver.market_trade_flow_15m;
DROP TABLE IF EXISTS silver.market_orderbook_metrics_15m;

COMMIT;
