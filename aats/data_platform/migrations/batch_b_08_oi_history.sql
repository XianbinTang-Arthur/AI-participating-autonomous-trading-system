-- Batch B · Stage 08 — P1-D Stage 5 OKX REST 历史 OI (open-interest) 回填 bronze 表
-- 参考: docs/design/p1d_okx_historical_backfill_plan_2026_04_20.md §3.1 + §13
--
-- 背景:
-- Phase 1A 的 `staging.market_oi_funding_ticks` 是实时 WS 的逐 tick 流, 只保留 7 天
-- retention (§6.5). 真正做 P1-D Phase 2A OI delta × sign(ΔP) 联合特征回归时, 需要
-- 30-90 天深度 OI 历史做统计稳健性校验, 只能走 OKX REST
-- `/api/v5/rubik/stat/contracts/open-interest-history` 批量回填.
--
-- 本 migration 建 1 张 Bronze 表:
--   bronze.market_oi_history_1h
--     symbol, ts (UTC 1h bar start), oi, oi_ccy, oi_usd,
--     ingest_run_id, received_at
--     PK (symbol, ts)
--
-- 说明:
--   - period 固化为 '1h': P1-D 预估 R² ≤ 0.02, 1h 粒度 已足 (§2.1 表),
--     且每 symbol × 90 天 只有 ~2160 rows, 一次性回填不碰 rate limit.
--   - oi_ccy: OKX 返回的 OI 计价 (币本位); oi_usd: 从 open-interest-usd endpoint
--     或由 oi_ccy × mark_price 推导, 本 stage 保留列但默认 NULL, 未来 Silver
--     ETL 或额外 backfill 可填.
--   - ingest_run_id: 关联 meta.ingest_runs, dataset_domain='microstructure'
--     (Phase 1A Stage 4 扩的白名单复用), trigger_mode='manual' (人工触发 backfill).
--
-- 幂等: CREATE TABLE IF NOT EXISTS. 重跑不 erase 既有数据.
--
-- 回滚见 batch_b_08_rollback.sql.

BEGIN;

-- 确保所需 schema 存在 (rdp_init_db 已建, 但防御性兜底幂等)
CREATE SCHEMA IF NOT EXISTS bronze;

-- ─────────────────────────────────────────────────────────────────────
-- 0. chk_cp_domain / chk_iri_domain 扩 'microstructure'
-- ─────────────────────────────────────────────────────────────────────
-- batch_b_07 只扩了 chk_ir_domain (ingest_runs) 和 chk_ir_trigger, 但漏了
--   - chk_cp_domain  on meta.ingest_checkpoints
--   - chk_iri_domain on meta.ingest_run_items
-- P1-D Stage 5 OI backfill 需要 upsert_checkpoint(dataset_domain='microstructure',
-- timeframe='oi_1h'), 因此必须把 'microstructure' 加进这两个 CHECK 白名单.
--
-- 幂等: DROP IF EXISTS + ADD 新约束.

ALTER TABLE meta.ingest_checkpoints DROP CONSTRAINT IF EXISTS chk_cp_domain;
ALTER TABLE meta.ingest_checkpoints ADD CONSTRAINT chk_cp_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text,
        'microstructure'::text
    ]));

ALTER TABLE meta.ingest_run_items DROP CONSTRAINT IF EXISTS chk_iri_domain;
ALTER TABLE meta.ingest_run_items ADD CONSTRAINT chk_iri_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text,
        'microstructure'::text
    ]));

-- ─────────────────────────────────────────────────────────────────────
-- bronze.market_oi_history_1h
--   来源: OKX REST /api/v5/rubik/stat/contracts/open-interest-history
--         period=1H, 每页 100 条, pagination begin/end by ts(ms)
--   natural PK: (symbol, ts) — 同一 symbol × 同一 1h bar 起点唯一
--   UPSERT 幂等: INSERT ... ON CONFLICT (PK) DO NOTHING
--   estimated: ~2160 rows × 90 days × 120 B ≈ 260 KB / symbol
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bronze.market_oi_history_1h (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,                -- 1h bar 起点 (UTC, OKX ts_ms 对齐)
    oi                       NUMERIC(28, 10) NOT NULL,             -- OI 张数 (contracts), OKX `oi`
    oi_ccy                   NUMERIC(28, 10),                       -- OI 基础货币量, OKX `oiCcy`
    oi_usd                   NUMERIC(28, 10),                       -- OI USD 估值 (optional, 回填 may not fill)

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);

CREATE INDEX IF NOT EXISTS idx_brz_oi_history_1h_ts
    ON bronze.market_oi_history_1h (ts);
CREATE INDEX IF NOT EXISTS idx_brz_oi_history_1h_sym_ts
    ON bronze.market_oi_history_1h (symbol, ts);

COMMIT;
