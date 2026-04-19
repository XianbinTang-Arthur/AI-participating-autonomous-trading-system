-- Batch B · Stage 09 — P1-D Stage 5 OKX REST 历史 mark-price candles + long-short ratio 回填 bronze 表
-- 参考: docs/design/p1d_okx_historical_backfill_plan_2026_04_20.md §3.2 §3.3 + §13
--
-- 背景:
-- Phase 1A `staging.market_oi_funding_ticks` 只保留 mark 的 WS tick 流 7 天;
-- Phase 2A 做 true basis = (perp - mark) 特征需 30 天 1m mark candles, 只能走
-- OKX REST `/api/v5/market/history-mark-price-candles` 批量回填. LS ratio 类似,
-- `/api/v5/rubik/stat/contracts/long-short-account-ratio` 需 30 天 5m 粒度.
--
-- 本 migration 建 2 张 Bronze 表:
--   bronze.market_mark_price_candles_1m
--     symbol, ts (1m bar start), open, high, low, close,
--     ingest_run_id, received_at
--     PK (symbol, ts)
--
--   bronze.market_long_short_ratio_5m
--     symbol, ts (5m bar start), ls_ratio_positions, ls_ratio_accounts,
--     ingest_run_id, received_at
--     PK (symbol, ts)
--
-- 说明:
--   - mark-price-candles-history 返回 [ts, open, high, low, close, confirm],
--     5 个价列全 required. confirm 列不入库 (回填只拿已确认 bar).
--   - long-short-account-ratio 按 OKX v5 文档返回 [ts, longShortRatio]; 部分
--     市场/时段也同时返回 position-based ratio. 我们 DDL 保留两个列都 nullable,
--     collector 按实际返回填.
--   - "symbol" 列语义:
--       mark-price-candles-history 直接用 instId (e.g. "BTC-USDT-SWAP")
--       long-short-account-ratio 用 ccy (e.g. "BTC"), 但为统一 Bronze
--       schema, 我们在写入时把 ccy 规范化为 "{ccy}-USDT-SWAP"
--       (参见 collector 代码 normalize_ls_symbol).
--   - ingest_run_id: dataset_domain='microstructure', trigger_mode='manual'.
--
-- 幂等: CREATE TABLE IF NOT EXISTS. 重跑不 erase 既有数据.
--
-- 回滚见 batch_b_09_rollback.sql.

BEGIN;

CREATE SCHEMA IF NOT EXISTS bronze;

-- ─────────────────────────────────────────────────────────────────────
-- A. bronze.market_mark_price_candles_1m
--   来源: OKX REST /api/v5/market/history-mark-price-candles
--         period=1m, 每页 100 条, pagination after/before by ts(ms)
--   natural PK: (symbol, ts)
--   estimated: ~43,200 rows × 30 days × 150 B ≈ 6.5 MB / symbol
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bronze.market_mark_price_candles_1m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,                -- 1m bar 起点 (UTC)
    open                     NUMERIC(20, 10) NOT NULL,
    high                     NUMERIC(20, 10) NOT NULL,
    low                      NUMERIC(20, 10) NOT NULL,
    close                    NUMERIC(20, 10) NOT NULL,

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);

CREATE INDEX IF NOT EXISTS idx_brz_mark_candles_1m_ts
    ON bronze.market_mark_price_candles_1m (ts);
CREATE INDEX IF NOT EXISTS idx_brz_mark_candles_1m_sym_ts
    ON bronze.market_mark_price_candles_1m (symbol, ts);

-- ─────────────────────────────────────────────────────────────────────
-- B. bronze.market_long_short_ratio_5m
--   来源: OKX REST /api/v5/rubik/stat/contracts/long-short-account-ratio
--         period=5m, 每页 100 条, pagination begin/end by ts(ms)
--   natural PK: (symbol, ts)
--   estimated: ~8640 rows × 30 days × 80 B ≈ 700 KB / symbol
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bronze.market_long_short_ratio_5m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,                -- 5m bar 起点 (UTC)
    ls_ratio_positions       NUMERIC(18, 10),                       -- position-based LS (部分市场提供)
    ls_ratio_accounts        NUMERIC(18, 10),                       -- account-based LS (OKX default field)

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);

CREATE INDEX IF NOT EXISTS idx_brz_ls_ratio_5m_ts
    ON bronze.market_long_short_ratio_5m (ts);
CREATE INDEX IF NOT EXISTS idx_brz_ls_ratio_5m_sym_ts
    ON bronze.market_long_short_ratio_5m (symbol, ts);

COMMIT;
