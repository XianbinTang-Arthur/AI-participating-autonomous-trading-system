-- Migration 0013: Create active parameter tables in governance schema
-- Active Parameters 落库: 将 JSON 文件存储迁移到数据库
--
-- Tables:
--   governance.active_parameter_sets      -- 每个 family/timeframe 组合的"当前生效"参数
--   governance.parameter_apply_history    -- 不可变审计日志 (apply / rollback / clear)

CREATE SCHEMA IF NOT EXISTS governance;

-- --------------------------------------------------------------------------
-- 1. governance.active_parameter_sets
--    每个 family/timeframe 组合最多一行，代表当前生效的参数集。
--    写入走 UPSERT (INSERT ... ON CONFLICT DO UPDATE) 保证原子性。
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS governance.active_parameter_sets (
    id                          SERIAL PRIMARY KEY,
    family                      VARCHAR(64)  NOT NULL,
    timeframe                   VARCHAR(16)  NOT NULL,
    parameter_set_id            VARCHAR(128) NOT NULL,
    values                      JSONB        NOT NULL,
    source_round_id             VARCHAR(128),
    approval_recommendation_id  VARCHAR(128),
    applied_by                  VARCHAR(128) NOT NULL DEFAULT 'operator',
    applied_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_active_combo UNIQUE (family, timeframe)
);

-- --------------------------------------------------------------------------
-- 2. governance.parameter_apply_history
--    不可变审计日志。只 INSERT，永不 UPDATE/DELETE。
--    记录每次 apply / rollback / clear 操作。
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS governance.parameter_apply_history (
    id                      SERIAL PRIMARY KEY,
    operation_id            VARCHAR(128)  NOT NULL UNIQUE,
    operation_type          VARCHAR(32)   NOT NULL,
    family                  VARCHAR(64)   NOT NULL,
    timeframe               VARCHAR(16)   NOT NULL,
    from_parameter_set_id   VARCHAR(128),
    to_parameter_set_id     VARCHAR(128),
    recommendation_id       VARCHAR(128),
    actor                   VARCHAR(128)  NOT NULL DEFAULT 'operator',
    notes                   TEXT,
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_apply_history_combo
    ON governance.parameter_apply_history (family, timeframe, created_at DESC);
