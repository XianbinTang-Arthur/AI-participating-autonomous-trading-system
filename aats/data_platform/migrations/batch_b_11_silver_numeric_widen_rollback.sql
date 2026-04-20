-- Batch B · Stage 11 rollback — Silver vol_weighted_tfi 精度回缩
--
-- 对偶 batch_b_11_silver_numeric_widen.sql: 把 NUMERIC(28, 10) 缩回
-- NUMERIC(14, 8)。 仅用于开发回归 / 回到 Stage 11 之前的 schema,
-- 一般生产不应回滚 (会再次触发 > 10^6 的 NumericValueOutOfRange)。
--
-- 注意:
--   若 silver.market_volume_profile_15m 已有 |vol_weighted_tfi| >= 10^6
--   的行, 本 rollback 会 raise NumericValueOutOfRange 并整个事务失败 ——
--   这是 PostgreSQL 强类型保护, 不应用 DELETE 强行绕过。回滚前需先手工
--   清理或 truncate 该列的溢出行。

BEGIN;

ALTER TABLE silver.market_volume_profile_15m
    ALTER COLUMN vol_weighted_tfi TYPE NUMERIC(14, 8);

COMMENT ON COLUMN silver.market_volume_profile_15m.vol_weighted_tfi IS
    'TFI × volume_ccy 交叉项';

COMMIT;
