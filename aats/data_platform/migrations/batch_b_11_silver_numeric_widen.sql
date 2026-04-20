-- Batch B · Stage 11 — P0-a Silver numeric widening (vol_weighted_tfi 溢出修复)
--
-- 起因:
--   live 环境 2026-04-20 05:45 UTC 之后 5 张 silver.market_*_15m 表全部停在
--   05:30, 而 governance.rdp_task_queue 每 15min 有新 task 全部 done/exit 0。
--   log_tail 内藏堆栈:
--     DataError('(psycopg.errors.NumericValueOutOfRange) numeric field overflow.
--     DETAIL: A field with precision 14, scale 8 must round to an absolute value
--     less than 10^6.')
--   溢出触发点: silver.market_volume_profile_15m.vol_weighted_tfi。
--
-- 根因分析:
--   vol_weighted_tfi = trade_flow_imbalance (∈ [-1, 1]) * volume_ccy
--   BTC-USDT-SWAP 15m 内 volume_ccy 轻松超过 10^6 USDT,
--   NUMERIC(14, 8) precision=14 scale=8 → |value| 必须 < 10^6 才能入库。
--   现实 volume 常态在 10^6 ~ 10^8 级 → 100% 持续溢出。
--
-- 扫描结论 (详见完工报告 §3):
--   本 migration 只扩一列: vol_weighted_tfi NUMERIC(14, 8) → NUMERIC(28, 10)
--   其他列均已在合理安全带 (z-score / bps / ratio / imbalance 数值范围小,
--   或 volume/depth 类已经用 NUMERIC(28, 10))。对齐 bronze.market_trades.sz
--   的 NUMERIC(28, 10) 精度, 给足头部尾部安全边际。
--
-- 回滚: batch_b_11_silver_numeric_widen_rollback.sql 把精度缩回 NUMERIC(14, 8)。
--   注意: 若运行中已写入 > 10^6 的值, rollback 会失败 —— 正常, 不应强回滚。

BEGIN;

-- vol_weighted_tfi: precision 14 scale 8 → precision 28 scale 10
-- Postgres ALTER COLUMN TYPE 对 NUMERIC 扩精度是 metadata-only 级操作
-- (scale 不减 + precision 增, 不会 rewrite rows), 在 live 大表上也秒级完成
ALTER TABLE silver.market_volume_profile_15m
    ALTER COLUMN vol_weighted_tfi TYPE NUMERIC(28, 10);

COMMENT ON COLUMN silver.market_volume_profile_15m.vol_weighted_tfi IS
    'TFI × volume_ccy 交叉项; NUMERIC(28,10) 精度 2026-04-20 P0-a 扩展前为 '
    'NUMERIC(14,8) 导致 > 10^6 的 15m 窗口溢出,现统一对齐 bronze.market_trades.sz';

COMMIT;
