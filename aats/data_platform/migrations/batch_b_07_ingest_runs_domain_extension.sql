-- Batch B · Stage 07 — P1-D Phase 1A ingest_runs CHECK 约束扩展
-- 参考: docs/review/p1d_phase1a_completion_2026_04_20.md / Phase 1A deploy retrospect
--
-- 背景:
-- Phase 1A microstructure collector 需要把 `ingest_run` 记录以:
--   - dataset_domain='microstructure'   (Phase 1A 首次引入)
--   - trigger_mode='daemon'             (独立常驻 collector, 非 scheduler/manual/auto_gap_repair)
-- 但既有 CHECK 约束(在 batch_b_01_core_schema 定义)只允许:
--   - domain IN ('candles', 'funding')
--   - trigger_mode IN ('scheduler', 'manual', 'auto_gap_repair')
--
-- Stage 1 / Stage 3 agent 在建 bronze/silver 表时未同步扩 ingest_runs 的白名单,
-- 导致 Phase 1A 首次 deploy 时 microstructure-collector insert ingest_run 触发
-- CheckViolation (chk_ir_domain 和 chk_ir_trigger).
--
-- 已在 live DB 手工 ALTER 恢复运行 (2026-04-20 deploy retro). 本 migration
-- 把修复 code-ize, 确保未来重建 DB 时自动生效.
--
-- 幂等: DROP CONSTRAINT IF EXISTS + ADD 新约束. 重跑不会出错.
--
-- 回滚见 batch_b_07_rollback.sql.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────
-- chk_ir_domain: 加 'microstructure'
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE meta.ingest_runs DROP CONSTRAINT IF EXISTS chk_ir_domain;
ALTER TABLE meta.ingest_runs ADD CONSTRAINT chk_ir_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text,
        'microstructure'::text
    ]));

-- ─────────────────────────────────────────────────────────────────────
-- chk_ir_trigger: 加 'daemon' (常驻 WS collector)
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE meta.ingest_runs DROP CONSTRAINT IF EXISTS chk_ir_trigger;
ALTER TABLE meta.ingest_runs ADD CONSTRAINT chk_ir_trigger
    CHECK (trigger_mode = ANY (ARRAY[
        'scheduler'::text,
        'manual'::text,
        'auto_gap_repair'::text,
        'daemon'::text
    ]));

COMMIT;
