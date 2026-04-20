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

-- 2026-04-20 code review B-M4: 加前置守卫.
-- 直接 ALTER 失败时错误消息不明 (只说 "numeric field overflow"), 运维得自己
-- 跑 SELECT 才能定位是哪些行溢出. 守卫里显式 COUNT + 精确指向 row, 运维
-- 直接按提示 clean 再 rollback, 减少误操作.
DO $$
DECLARE
    offending_count bigint;
    sample_ts timestamptz;
    sample_val numeric;
BEGIN
    SELECT COUNT(*), MIN(ts), MIN(vol_weighted_tfi)
    INTO offending_count, sample_ts, sample_val
    FROM silver.market_volume_profile_15m
    WHERE ABS(vol_weighted_tfi) >= 1e6;

    IF offending_count > 0 THEN
        RAISE EXCEPTION
            'Rollback blocked: silver.market_volume_profile_15m 有 % 行 |vol_weighted_tfi| >= 10^6 (老精度 NUMERIC(14,8) 的溢出阈值). '
            '样本: ts=%, value=%. 这些行在 Stage 11 扩精度后写入, 回缩精度会 NumericValueOutOfRange. '
            '处理路径 (择一): '
            '(a) 先 UPDATE SET vol_weighted_tfi = NULL WHERE ABS(vol_weighted_tfi) >= 1e6 再 rollback; '
            '(b) 先 DELETE 溢出行再 rollback (会丢数据); '
            '(c) 放弃 rollback, 保留 NUMERIC(28,10). '
            '任何方案落地前都写决策 audit 到 docs/review/.',
            offending_count, sample_ts, sample_val;
    END IF;
END $$;

ALTER TABLE silver.market_volume_profile_15m
    ALTER COLUMN vol_weighted_tfi TYPE NUMERIC(14, 8);

COMMENT ON COLUMN silver.market_volume_profile_15m.vol_weighted_tfi IS
    'TFI × volume_ccy 交叉项';

COMMIT;
