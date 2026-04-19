-- Batch B · Stage 06 — P1-D Phase 1A Silver microstructure 15m 聚合层
-- 参考: docs/design/p1d_phase1a_implementation_design_2026_04_20.md §5
--
-- 本 migration 建 5 张 Silver 15m 表,供 Phase 1A 的 microstructure Silver
-- ETL (build_silver_microstructure_15m) 聚合 Bronze/staging 数据落地:
--   - silver.market_orderbook_metrics_15m  (§5.1)
--   - silver.market_trade_flow_15m         (§5.2)
--   - silver.market_oi_funding_metrics_15m (§5.3)
--   - silver.market_volume_profile_15m     (§5.4)
--   - silver.market_liquidation_metrics_15m (§5.5)
--
-- 所有 5 张表共用规范:
--   PK = (symbol, ts) 其中 ts = 15m bar 起点 (UTC 对齐)
--   footer: ingest_run_id / dataset_version / quality_flags / created_at / updated_at
--   UPSERT: ON CONFLICT (symbol, ts) DO UPDATE SET ... , updated_at=EXCLUDED.updated_at
--   quality_flags 合法值: etl_failed / partial_data / gap_filled_with_nulls
--                        / stale_source / whale_threshold_reinit / orderbook_*_no_data
--                        / ema_seed_from_sma / partial_baseline / liquidation_no_data
--   index: idx_slv_<name>_ts  和  idx_slv_<name>_ver
--
-- 决策来源 (附录 E):
--   - #3 走 meta.ingest_runs 追溯 (跟 daily_ingest 一致)
--   - #4 Phase 1A 不预留 multi-horizon 表, 只建 _15m
--   - #7 workflow name = microstructure_silver_15m
--
-- 回滚见 batch_b_06_silver_microstructure_rollback.sql。

BEGIN;

-- 防御性 schema 兜底 (rdp_init_db 通常已建, 但 migration 独立跑也不报错)
CREATE SCHEMA IF NOT EXISTS silver;

-- ─────────────────────────────────────────────────────────────────────
-- A. silver.market_orderbook_metrics_15m (§5.1)
--   来源: bronze.market_orderbook_bbo + bronze.market_orderbook_books5
--   聚合: BBO 1Hz 采样 + books5 2Hz 采样 的 15min 窗口统计
--   估算: ~500 KB/30d, ~6 MB/365d (可忽略)
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS silver.market_orderbook_metrics_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- BBO level (from bbo-tbt sampled @ 1Hz into bronze)
    bbo_imbalance_mean       NUMERIC(12, 8),
    bbo_imbalance_std        NUMERIC(12, 8),
    bbo_imbalance_last       NUMERIC(12, 8),
    bbo_samples_n            INTEGER      NOT NULL DEFAULT 0,

    -- Top-5 level (from books5 sampled @ 2Hz into bronze)
    top5_bid_depth_ccy       NUMERIC(28, 10),
    top5_ask_depth_ccy       NUMERIC(28, 10),
    top5_imbalance_mean      NUMERIC(12, 8),
    top5_imbalance_ema       NUMERIC(12, 8),
    top5_weighted_imbalance  NUMERIC(12, 8),
    books5_samples_n         INTEGER      NOT NULL DEFAULT 0,

    -- Spread metrics
    spread_bps_mean          NUMERIC(12, 4),
    spread_bps_max           NUMERIC(12, 4),
    spread_bps_min           NUMERIC(12, 4),

    -- Mid anchor for downstream joins
    mid_price_last           NUMERIC(20, 10),

    -- 共用 footer
    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_orderbook_15m_ts
    ON silver.market_orderbook_metrics_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_orderbook_15m_ver
    ON silver.market_orderbook_metrics_15m (dataset_version);

COMMENT ON TABLE silver.market_orderbook_metrics_15m IS
    'P1-D Phase 1A Silver 15m — BBO + top5 orderbook aggregated metrics';
COMMENT ON COLUMN silver.market_orderbook_metrics_15m.ts IS
    '15m bar 起点 (UTC 对齐),对应 (bar_start_ts, bar_start_ts+15min) 窗口';
COMMENT ON COLUMN silver.market_orderbook_metrics_15m.top5_weighted_imbalance IS
    '用 bid_depth/ask_depth 加权的 imbalance,对深单更敏感';

-- ─────────────────────────────────────────────────────────────────────
-- B. silver.market_trade_flow_15m (§5.2)
--   来源: bronze.market_trades
--   聚合: 15m 窗口的 trade volume + 大单检测 + VWAP 对 mid 偏移
--   估算: ~640 KB/30d, ~8 MB/365d
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS silver.market_trade_flow_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- Volume (ccy = quote currency = USDT for BTC-USDT-SWAP)
    total_volume_ccy         NUMERIC(28, 10),
    buy_volume_ccy           NUMERIC(28, 10),
    sell_volume_ccy          NUMERIC(28, 10),
    trade_count              INTEGER      NOT NULL DEFAULT 0,

    -- Aggressor flow (taker = OKX side field 的语义)
    taker_buy_ratio          NUMERIC(12, 8),
    trade_flow_imbalance     NUMERIC(12, 8),
    log_tfi                  NUMERIC(12, 8),

    -- Size distribution
    mean_trade_size          NUMERIC(18, 8),
    p50_trade_size           NUMERIC(18, 8),
    p95_trade_size           NUMERIC(18, 8),
    p99_trade_size           NUMERIC(18, 8),
    max_trade_size           NUMERIC(18, 8),

    -- Whale detection (size > rolling_1h.p99 threshold)
    whale_threshold_applied  NUMERIC(18, 8),
    whale_count              INTEGER      NOT NULL DEFAULT 0,
    whale_buy_volume_ccy     NUMERIC(28, 10),
    whale_sell_volume_ccy    NUMERIC(28, 10),
    whale_direction          NUMERIC(12, 8),

    -- Aggressiveness
    vwap                     NUMERIC(20, 10),
    mid_price_ref            NUMERIC(20, 10),
    vwap_minus_mid_bps       NUMERIC(12, 4),

    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_trade_flow_15m_ts
    ON silver.market_trade_flow_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_trade_flow_15m_ver
    ON silver.market_trade_flow_15m (dataset_version);

COMMENT ON TABLE silver.market_trade_flow_15m IS
    'P1-D Phase 1A Silver 15m — trade flow, taker buy/sell split, whale detection';
COMMENT ON COLUMN silver.market_trade_flow_15m.log_tfi IS
    'log(buy/sell) clipped to [-5, 5]; 符号表示 taker 主导方向';
COMMENT ON COLUMN silver.market_trade_flow_15m.vwap_minus_mid_bps IS
    '+ 值表示 taker buy 侧主导,- 值表示 taker sell 侧主导';

-- ─────────────────────────────────────────────────────────────────────
-- C. silver.market_oi_funding_metrics_15m (§5.3)
--   来源: staging.market_oi_funding_ticks (tick_type IN ('oi','funding','mark'))
--         + silver.market_orderbook_metrics_15m (本 bar 的 mid_price_last)
--   聚合: OI 四价 + EMA-20 + funding z-score 7d + mark/mid basis
--   估算: ~640 KB/30d, ~8 MB/365d
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS silver.market_oi_funding_metrics_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- OI (from WS open-interest @ 3s, bucketed to 15m window)
    oi_open                  NUMERIC(28, 10),
    oi_close                 NUMERIC(28, 10),
    oi_high                  NUMERIC(28, 10),
    oi_low                   NUMERIC(28, 10),
    oi_delta                 NUMERIC(18, 10),
    oi_samples_n             INTEGER      NOT NULL DEFAULT 0,

    -- EMA-20 of 15m bars (rolling, source from self-previous rows)
    oi_ema_20                NUMERIC(28, 10),
    oi_delta_vs_ema          NUMERIC(18, 10),

    -- Price-OI joint regime
    price_change_bps         NUMERIC(12, 4),
    oi_price_regime          TEXT,

    -- Funding (from WS funding-rate, 1/min updates, last-value-wins per bar)
    funding_rate_current     NUMERIC(18, 12),
    funding_rate_next_est    NUMERIC(18, 12),
    funding_z_score_7d       NUMERIC(12, 6),
    funding_deviation_30d    NUMERIC(18, 12),
    minutes_to_next_funding  INTEGER,

    -- Mark / basis (from WS mark-price + orderbook silver's mid)
    mark_price               NUMERIC(20, 10),
    mid_price_ref            NUMERIC(20, 10),
    basis_bps                NUMERIC(12, 4),

    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_oi_funding_15m_ts
    ON silver.market_oi_funding_metrics_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_oi_funding_15m_ver
    ON silver.market_oi_funding_metrics_15m (dataset_version);

COMMENT ON TABLE silver.market_oi_funding_metrics_15m IS
    'P1-D Phase 1A Silver 15m — OI/funding/mark metrics with EMA + z-score';
COMMENT ON COLUMN silver.market_oi_funding_metrics_15m.oi_price_regime IS
    'trend_long / trend_short / short_cover / long_cover / mixed / flat';

-- ─────────────────────────────────────────────────────────────────────
-- D. silver.market_volume_profile_15m (§5.4)
--   来源: bronze.market_trades + 历史 silver.market_trade_flow_15m 同时段
--   聚合: 本 bar volume vs (dow × hod × 15min slot) 4-week rolling baseline
--   估算: ~430 KB/30d, ~5 MB/365d
--   冷启动: baseline_sample_weeks < 4 时 volume_z_score=NULL + quality_flags
--          += 'partial_baseline'
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS silver.market_volume_profile_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- 本 bar volume
    volume_ccy               NUMERIC(28, 10),
    trade_count              INTEGER      NOT NULL DEFAULT 0,

    -- Seasonal baseline (dow × hod × 15min slot) 4-week rolling
    expected_volume_ccy      NUMERIC(28, 10),
    expected_volume_std      NUMERIC(28, 10),
    volume_z_score           NUMERIC(12, 6),
    volume_spike_flag        BOOLEAN      NOT NULL DEFAULT FALSE,
    dow_hod_slot             TEXT,

    -- Interaction (with TFI cross-product)
    vol_weighted_tfi         NUMERIC(14, 8),

    -- Baseline cold-start diagnostic
    baseline_sample_weeks    INTEGER      NOT NULL DEFAULT 0,

    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_volume_profile_15m_ts
    ON silver.market_volume_profile_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_volume_profile_15m_ver
    ON silver.market_volume_profile_15m (dataset_version);

COMMENT ON TABLE silver.market_volume_profile_15m IS
    'P1-D Phase 1A Silver 15m — volume vs seasonal baseline z-score';
COMMENT ON COLUMN silver.market_volume_profile_15m.dow_hod_slot IS
    '溯源 key: 如 "mon_13:00" 表示星期一 13:00 UTC 的 15min 槽位';
COMMENT ON COLUMN silver.market_volume_profile_15m.baseline_sample_weeks IS
    '0-4; 冷启动阶段不足 4 周时 z_score=NULL 并打 partial_baseline flag';

-- ─────────────────────────────────────────────────────────────────────
-- E. silver.market_liquidation_metrics_15m (§5.5)
--   来源: staging.raw_liquidations (inst_id = symbol 直接映射)
--   聚合: long/short 清算计数 + notional + cascade 检测 + 7d z-score
--   估算: ~430 KB/30d, ~5 MB/365d
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS silver.market_liquidation_metrics_15m (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,

    -- Counts
    long_liq_count           INTEGER      NOT NULL DEFAULT 0,
    short_liq_count          INTEGER      NOT NULL DEFAULT 0,

    -- Notional (bk_px * sz, USD approximation for BTC-USDT-SWAP)
    long_liq_notional_usd    NUMERIC(28, 10),
    short_liq_notional_usd   NUMERIC(28, 10),
    liq_imbalance            NUMERIC(12, 8),
    max_single_liq_usd       NUMERIC(28, 10),

    -- Cascade detection
    cascade_flag             BOOLEAN      NOT NULL DEFAULT FALSE,
    cascade_threshold_used   INTEGER,
    intensity_z_7d           NUMERIC(12, 6),

    ingest_run_id            UUID         NOT NULL,
    dataset_version          TEXT         NOT NULL,
    quality_flags            TEXT[]       NOT NULL DEFAULT '{}'::text[],
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_slv_micro_liq_metrics_15m_ts
    ON silver.market_liquidation_metrics_15m (ts);
CREATE INDEX IF NOT EXISTS idx_slv_micro_liq_metrics_15m_ver
    ON silver.market_liquidation_metrics_15m (dataset_version);

COMMENT ON TABLE silver.market_liquidation_metrics_15m IS
    'P1-D Phase 1A Silver 15m — liquidation intensity + cascade detection';
COMMENT ON COLUMN silver.market_liquidation_metrics_15m.liq_imbalance IS
    '(long_usd - short_usd) / total_usd; 符号表示主导方向';

COMMIT;
