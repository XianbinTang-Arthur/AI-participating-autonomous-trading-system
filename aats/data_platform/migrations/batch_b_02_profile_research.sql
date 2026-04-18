-- Batch B · Stage 02 — Phase 1 profile research tables
-- 参考: docs/task/rdp_scope_expansion_detailed_design_v3.md §1.1
--
-- 依赖: batch_b_01_core_schema.sql 已运行(需要 governance schema 的 scope 列)
-- 回滚: batch_b_02_rollback.sql

BEGIN;

-- Profile-level research 运行记录
CREATE TABLE IF NOT EXISTS governance.profile_research_runs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(128) NOT NULL UNIQUE,
    profile_id VARCHAR(64) NOT NULL,
    oos_window_days INTEGER NOT NULL DEFAULT 90,
    grid_size INTEGER NOT NULL,
    grid_method VARCHAR(32) NOT NULL DEFAULT 'product',
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    recommendation_id VARCHAR(128),
    rejected_by_clamp BOOLEAN NOT NULL DEFAULT FALSE,
    clamp_violation_direction VARCHAR(16),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS ix_profile_research_profile_started
    ON governance.profile_research_runs(profile_id, started_at DESC);

-- profile_type_review streak — R1-08 CAS + R2-02 mixed 支持
CREATE TABLE IF NOT EXISTS governance.profile_type_review_streak (
    profile_id VARCHAR(64) PRIMARY KEY,
    clamp_violation_direction VARCHAR(16) NOT NULL,
    streak_count INTEGER NOT NULL DEFAULT 0,
    last_run_id VARCHAR(128) NOT NULL,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    review_recommendation_id VARCHAR(128),
    CONSTRAINT chk_streak_direction CHECK (
        clamp_violation_direction IN ('above_upper', 'below_lower', 'mixed')
    )
);

COMMIT;
