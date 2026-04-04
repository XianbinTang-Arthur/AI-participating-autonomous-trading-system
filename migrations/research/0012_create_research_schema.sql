-- Migration 0012: Create research schema for Phase 2 Parameter Research Platform
-- Phase 2: Parameter Research Platform
-- Creates the research schema and experiment metadata tables.
--
-- Tables:
--   research.experiments            -- 实验主记录
--   research.experiment_summaries   -- 实验摘要（诊断指标快照）
--   research.parameter_scan_runs    -- 参数扫描批次

CREATE SCHEMA IF NOT EXISTS research;

-- --------------------------------------------------------------------------
-- 1. research.experiments
--    每次 replay 实验的元数据和产物路径。
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research.experiments (
    experiment_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family              TEXT NOT NULL CHECK (family IN ('independent', 'directional')),
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL CHECK (timeframe IN ('1m', '5m', '15m', '1H')),
    dataset_version     TEXT NOT NULL,
    parameter_overrides JSONB NOT NULL DEFAULT '{}',
    window_start_ts     TIMESTAMPTZ NULL,
    window_end_ts       TIMESTAMPTZ NULL,
    bar_count           INT NULL,
    status              TEXT NOT NULL CHECK (status IN (
                            'pending', 'running', 'succeeded', 'failed'
                        )) DEFAULT 'pending',
    error_message       TEXT NULL,
    result_path         TEXT NULL,
    summary_path        TEXT NULL,
    report_path         TEXT NULL,
    scan_run_id         UUID NULL,          -- 若由 parameter scan 生成，关联 scan_run
    notes               TEXT NULL,
    started_at          TIMESTAMPTZ NULL,
    finished_at         TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_exp_family     ON research.experiments (family, symbol, timeframe);
CREATE INDEX IF NOT EXISTS idx_exp_status     ON research.experiments (status);
CREATE INDEX IF NOT EXISTS idx_exp_scan       ON research.experiments (scan_run_id) WHERE scan_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_exp_version    ON research.experiments (dataset_version);

-- --------------------------------------------------------------------------
-- 2. research.experiment_summaries
--    存放关键诊断指标快照，便于快速比较。
--    逐 bar replay 明细仍落文件（parquet / csv），不进数据库。
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research.experiment_summaries (
    experiment_summary_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id                   UUID NOT NULL REFERENCES research.experiments(experiment_id),
    total_bars                      INT NOT NULL DEFAULT 0,
    opening_count                   INT NOT NULL DEFAULT 0,
    blocked_count                   INT NOT NULL DEFAULT 0,
    hold_count                      INT NOT NULL DEFAULT 0,
    close_count                     INT NOT NULL DEFAULT 0,
    selectable_count                INT NOT NULL DEFAULT 0,
    execution_compatible_count      INT NOT NULL DEFAULT 0,
    selectable_ratio                DOUBLE PRECISION NULL,
    execution_compatible_ratio      DOUBLE PRECISION NULL,
    mean_long_score                 DOUBLE PRECISION NULL,
    mean_short_score                DOUBLE PRECISION NULL,
    mean_expected_edge_bps          DOUBLE PRECISION NULL,
    median_expected_edge_bps        DOUBLE PRECISION NULL,
    p25_expected_edge_bps           DOUBLE PRECISION NULL,
    p75_expected_edge_bps           DOUBLE PRECISION NULL,
    top_blocking_reasons            JSONB NULL,         -- [{"reason": "...", "count": N}, ...]
    state_distribution              JSONB NULL,         -- {"flat": N, "probing": N, ...}
    action_distribution             JSONB NULL,         -- {"open": N, "hold": N, "blocked": N, ...}
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_expsum_exp ON research.experiment_summaries (experiment_id);

-- --------------------------------------------------------------------------
-- 3. research.parameter_scan_runs
--    记录一次参数扫描批次。一个 scan_run 包含多个 experiment。
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research.parameter_scan_runs (
    scan_run_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family              TEXT NOT NULL CHECK (family IN ('independent', 'directional')),
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL CHECK (timeframe IN ('1m', '5m', '15m', '1H')),
    dataset_version     TEXT NOT NULL,
    parameter_grid      JSONB NOT NULL,     -- {"min_confirm_ticks": [2,3,4], ...}
    total_combinations  INT NOT NULL DEFAULT 0,
    completed_count     INT NOT NULL DEFAULT 0,
    failed_count        INT NOT NULL DEFAULT 0,
    status              TEXT NOT NULL CHECK (status IN (
                            'pending', 'running', 'succeeded', 'failed'
                        )) DEFAULT 'pending',
    comparison_path     TEXT NULL,           -- comparison summary 产物路径
    notes               TEXT NULL,
    started_at          TIMESTAMPTZ NULL,
    finished_at         TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_psr_status ON research.parameter_scan_runs (status);
CREATE INDEX IF NOT EXISTS idx_psr_family ON research.parameter_scan_runs (family, symbol, timeframe);

-- FK: experiments.scan_run_id -> parameter_scan_runs.scan_run_id
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_exp_scan'
    ) THEN
        ALTER TABLE research.experiments
            ADD CONSTRAINT fk_exp_scan
            FOREIGN KEY (scan_run_id) REFERENCES research.parameter_scan_runs(scan_run_id)
            ON DELETE SET NULL;
    END IF;
END
$$;
