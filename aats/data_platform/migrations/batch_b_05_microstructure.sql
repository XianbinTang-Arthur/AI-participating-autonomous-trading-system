-- Batch B · Stage 05 — P1-D Phase 1A microstructure Bronze + staging 表
-- 参考: docs/design/p1d_phase1a_implementation_design_2026_04_20.md §6
--
-- 本 migration 建 3 张 Bronze 表 + 1 张 staging tick 表,供 Phase 1A 的
-- microstructure collector 从 OKX 三个 WS 频道 (trades-all / bbo-tbt /
-- books5) 落库使用,以及 open-interest / funding / mark-price 的 tick 级
-- staging 表供 Silver ETL 聚合。
--
-- 决策来源: §12 附录 E 的 8 项 default:
--   - (symbol, ts, trade_id) 作 bronze.market_trades 的 PK(C.2 主观判断)
--   - bbo 客户端 1Hz 采样(疑问 #5 default)
--   - 仅结构化字段 + raw_payload JSONB(不整包保留 arg 等无用字段)
--   - retention 分级: market_trades 30d / bbo 14d / books5 14d / oi_funding 7d
--     (retention 由 daily housekeeping job 或后续加 partitioned retention 做,
--      本 migration 只建表; §6.5)
--
-- 回滚见 batch_b_05_rollback.sql。

BEGIN;

-- 确保所需 schema 存在 (rdp_init_db 通常已建,但防御性兜底幂等)
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS staging;

-- ─────────────────────────────────────────────────────────────────────
-- A. bronze.market_trades (§6.1)
--   来源: OKX `trades-all` WS 频道
--   natural PK: (symbol, ts, trade_id)
--   UPSERT 幂等: ON CONFLICT (PK) DO NOTHING
--   estimated: ~300MB/day/symbol @ BTC-USDT-SWAP, 30d retention
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bronze.market_trades (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,                -- OKX trade.ts (ms → utc)
    trade_id                 TEXT         NOT NULL,                -- OKX tradeId, string
    px                       NUMERIC(20, 10) NOT NULL,
    sz                       NUMERIC(28, 10) NOT NULL,
    side                     TEXT         NOT NULL,                -- 'buy' or 'sell' (taker side per OKX)
    raw_payload              JSONB,                                 -- 仅保留 OKX detail, 不含 arg

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts, trade_id),
    CONSTRAINT chk_brz_trades_side CHECK (side IN ('buy', 'sell'))
);
CREATE INDEX IF NOT EXISTS idx_brz_market_trades_ts
    ON bronze.market_trades (ts);
CREATE INDEX IF NOT EXISTS idx_brz_market_trades_sym_ts
    ON bronze.market_trades (symbol, ts);
-- trade_id 不加独立索引: PK 里已含,且 symbol 是强过滤
-- (symbol, ts) 索引支持 ETL 的 bar 窗口扫描(这是热路径)

-- ─────────────────────────────────────────────────────────────────────
-- B. bronze.market_orderbook_bbo (§6.2)
--   来源: OKX `bbo-tbt` WS 频道 (10ms 推送)
--   客户端采样: 1 Hz (1 行/秒/symbol)
--   natural PK: (symbol, ts)
--   GENERATED STORED 列预算 mid / spread / imbalance 避免 Silver ETL 重算
--   estimated: ~11MB/day, 14d retention
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bronze.market_orderbook_bbo (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,                -- 采样时刻(客户端)
    source_ts                TIMESTAMPTZ  NOT NULL,                -- OKX 推送原 ts
    bid_px                   NUMERIC(20, 10) NOT NULL,
    bid_sz                   NUMERIC(28, 10) NOT NULL,
    ask_px                   NUMERIC(20, 10) NOT NULL,
    ask_sz                   NUMERIC(28, 10) NOT NULL,
    -- 便利性计算字段(避免 Silver ETL 每次重算)
    mid                      NUMERIC(20, 10) GENERATED ALWAYS AS ((bid_px + ask_px) / 2) STORED,
    spread                   NUMERIC(20, 10) GENERATED ALWAYS AS (ask_px - bid_px) STORED,
    imbalance                NUMERIC(18, 10) GENERATED ALWAYS AS (
        CASE WHEN (bid_sz + ask_sz) > 0
             THEN (bid_sz - ask_sz) / (bid_sz + ask_sz)
             ELSE 0 END
    ) STORED,

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_brz_market_orderbook_bbo_ts
    ON bronze.market_orderbook_bbo (ts);

-- ─────────────────────────────────────────────────────────────────────
-- C. bronze.market_orderbook_books5 (§6.3)
--   来源: OKX `books5` WS 频道 (100ms 推送)
--   客户端采样: 2 Hz (500ms)
--   natural PK: (symbol, ts)
--   5 个级别展平(避免 JSONB 解析成本)
--   estimated: ~48MB/day, 14d retention
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bronze.market_orderbook_books5 (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,                -- 采样时刻
    source_ts                TIMESTAMPTZ  NOT NULL,                -- OKX 推送原 ts
    -- 5 个级别展平(避免 JSONB 解析成本)
    bid_px_1                 NUMERIC(20, 10) NOT NULL,
    bid_sz_1                 NUMERIC(28, 10) NOT NULL,
    bid_px_2                 NUMERIC(20, 10),
    bid_sz_2                 NUMERIC(28, 10),
    bid_px_3                 NUMERIC(20, 10),
    bid_sz_3                 NUMERIC(28, 10),
    bid_px_4                 NUMERIC(20, 10),
    bid_sz_4                 NUMERIC(28, 10),
    bid_px_5                 NUMERIC(20, 10),
    bid_sz_5                 NUMERIC(28, 10),
    ask_px_1                 NUMERIC(20, 10) NOT NULL,
    ask_sz_1                 NUMERIC(28, 10) NOT NULL,
    ask_px_2                 NUMERIC(20, 10),
    ask_sz_2                 NUMERIC(28, 10),
    ask_px_3                 NUMERIC(20, 10),
    ask_sz_3                 NUMERIC(28, 10),
    ask_px_4                 NUMERIC(20, 10),
    ask_sz_4                 NUMERIC(28, 10),
    ask_px_5                 NUMERIC(20, 10),
    ask_sz_5                 NUMERIC(28, 10),

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_brz_market_orderbook_books5_ts
    ON bronze.market_orderbook_books5 (ts);

-- ─────────────────────────────────────────────────────────────────────
-- D. staging.market_oi_funding_ticks (§6.4)
--   为什么放 staging: 和 staging.raw_liquidations 一样, 这是每 tick
--   插入的原始流, Silver ETL 直接 group-by 聚合成 Silver, 不需要独立
--   bronze 精简层。
--   BIGSERIAL id PK 避免 (ts, symbol, tick_type) 同一 ms 多 tick 冲突。
--   tick_type ∈ {'oi','funding','mark'} 三类,按类型解释列含义。
--   estimated: ~30MB/day, 7d retention
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS staging.market_oi_funding_ticks (
    id                       BIGSERIAL    PRIMARY KEY,
    ts                       TIMESTAMPTZ  NOT NULL,                -- OKX 推送 ts
    symbol                   TEXT         NOT NULL,
    tick_type                TEXT         NOT NULL,                -- 'oi' | 'funding' | 'mark'
    oi                       NUMERIC(28, 10),                      -- when tick_type='oi'
    oi_ccy                   NUMERIC(28, 10),
    funding_rate             NUMERIC(18, 12),                      -- when tick_type='funding'
    next_funding_rate        NUMERIC(18, 12),
    next_funding_time        TIMESTAMPTZ,
    mark_px                  NUMERIC(20, 10),                      -- when tick_type='mark'

    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT chk_staging_oif_type CHECK (tick_type IN ('oi', 'funding', 'mark'))
);
CREATE INDEX IF NOT EXISTS ix_staging_market_oif_sym_ts
    ON staging.market_oi_funding_ticks (symbol, ts);
CREATE INDEX IF NOT EXISTS ix_staging_market_oif_type_ts
    ON staging.market_oi_funding_ticks (tick_type, ts);

COMMIT;
