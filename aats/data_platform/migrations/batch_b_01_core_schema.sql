-- Batch B · Stage 01 — scope 列 + system_config + apply saga idempotency + heartbeat
-- 参考: docs/task/rdp_scope_expansion_detailed_design_v3.md §1.1
--
-- 本 migration 是所有 Phase (1/2/3) 的基建,必须最先跑。
-- 回滚见 batch_b_01_rollback.sql。

BEGIN;

-- A. recommendations 加 scope + scope_ref
ALTER TABLE governance.recommendations
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_rec_scope_ref
    ON governance.recommendations(scope, scope_ref, status);

ALTER TABLE governance.recommendations
    DROP CONSTRAINT IF EXISTS chk_rec_scope;
ALTER TABLE governance.recommendations
    ADD CONSTRAINT chk_rec_scope CHECK (
        scope IN ('combo', 'profile', 'sleeve', 'risk')
    );

ALTER TABLE governance.recommendations
    DROP CONSTRAINT IF EXISTS chk_rec_scope_fields;
ALTER TABLE governance.recommendations
    ADD CONSTRAINT chk_rec_scope_fields CHECK (
        CASE
          WHEN scope = 'combo' THEN family IS NOT NULL AND timeframe IS NOT NULL
          WHEN scope IN ('profile', 'sleeve') THEN scope_ref IS NOT NULL
          ELSE TRUE
        END
    );

-- B. parameter_sets 同构改
ALTER TABLE governance.parameter_sets
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

ALTER TABLE governance.parameter_sets
    ALTER COLUMN family DROP NOT NULL,
    ALTER COLUMN timeframe DROP NOT NULL;

ALTER TABLE governance.parameter_sets
    DROP CONSTRAINT IF EXISTS chk_ps_scope_fields;
ALTER TABLE governance.parameter_sets
    ADD CONSTRAINT chk_ps_scope_fields CHECK (
        CASE
          WHEN scope = 'combo' THEN family IS NOT NULL AND timeframe IS NOT NULL
          WHEN scope IN ('profile', 'sleeve') THEN scope_ref IS NOT NULL
          ELSE TRUE
        END
    );

-- C. active_parameter_sets — 核心修正 R1-01
ALTER TABLE governance.active_parameter_sets
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

ALTER TABLE governance.active_parameter_sets
    ALTER COLUMN family DROP NOT NULL,
    ALTER COLUMN timeframe DROP NOT NULL;

-- 原 uq_active_combo 是 CONSTRAINT,必须 DROP CONSTRAINT
ALTER TABLE governance.active_parameter_sets
    DROP CONSTRAINT IF EXISTS uq_active_combo;

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_combo
    ON governance.active_parameter_sets(family, timeframe)
    WHERE scope = 'combo';
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_profile
    ON governance.active_parameter_sets(scope_ref)
    WHERE scope = 'profile';
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_sleeve
    ON governance.active_parameter_sets(scope_ref)
    WHERE scope = 'sleeve';

-- D. parameter_apply_history — 加 scope (R1-11)
ALTER TABLE governance.parameter_apply_history
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

-- profile / sleeve scope 下 family/timeframe 可为 NULL(用 scope_ref 替代)
ALTER TABLE governance.parameter_apply_history
    ALTER COLUMN family DROP NOT NULL,
    ALTER COLUMN timeframe DROP NOT NULL;

-- scope 一致性:combo 必带 family/timeframe;profile/sleeve 必带 scope_ref
ALTER TABLE governance.parameter_apply_history
    DROP CONSTRAINT IF EXISTS chk_apply_history_scope_fields;
ALTER TABLE governance.parameter_apply_history
    ADD CONSTRAINT chk_apply_history_scope_fields CHECK (
        CASE
          WHEN scope = 'combo'         THEN family IS NOT NULL AND timeframe IS NOT NULL
          WHEN scope IN ('profile', 'sleeve') THEN scope_ref IS NOT NULL
          ELSE TRUE
        END
    );

CREATE INDEX IF NOT EXISTS ix_apply_history_scope_ref
    ON governance.parameter_apply_history(scope, scope_ref, created_at DESC);

-- E. system_config (R1-06, R2-06: version CAS + 审计)
CREATE TABLE IF NOT EXISTS governance.system_config (
    key          VARCHAR(128) PRIMARY KEY,
    value        JSONB        NOT NULL,
    version      INTEGER      NOT NULL DEFAULT 1,
    updated_by   VARCHAR(128) NOT NULL,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS governance.system_config_history (
    id           BIGSERIAL PRIMARY KEY,
    key          VARCHAR(128) NOT NULL,
    old_value    JSONB,
    new_value    JSONB        NOT NULL,
    old_version  INTEGER,
    new_version  INTEGER      NOT NULL,
    changed_by   VARCHAR(128) NOT NULL,
    changed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_sc_history_key_time
    ON governance.system_config_history(key, changed_at DESC);

-- seed feature flags
INSERT INTO governance.system_config(key, value, version, updated_by, notes) VALUES
    ('profile_upgrade_auto_apply_enabled', 'false'::jsonb, 1, 'migration', 'Phase 1 shadow flag'),
    ('cost_calibration_auto_recommend_enabled', 'false'::jsonb, 1, 'migration', 'Phase 2 shadow flag'),
    ('sleeve_budget_advice_enabled', 'true'::jsonb, 1, 'migration', 'Phase 3 observation-only 默认开')
ON CONFLICT (key) DO NOTHING;

-- F. rdp_daemon_heartbeat (R2-15: 独立单行表)
CREATE TABLE IF NOT EXISTS governance.rdp_daemon_heartbeat (
    singleton_key VARCHAR(32) PRIMARY KEY,
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pid           INTEGER,
    version       VARCHAR(64),
    CONSTRAINT chk_heartbeat_singleton CHECK (singleton_key = 'rdp_daemon')
);

INSERT INTO governance.rdp_daemon_heartbeat(singleton_key)
VALUES ('rdp_daemon') ON CONFLICT DO NOTHING;

-- G. apply_saga_operations (R2-08: operation_id 幂等)
CREATE TABLE IF NOT EXISTS governance.apply_saga_operations (
    operation_id         VARCHAR(64) PRIMARY KEY,
    recommendation_id    VARCHAR(128) NOT NULL,
    scope                VARCHAR(32) NOT NULL,
    step1_done_at        TIMESTAMPTZ,
    step2_done_at        TIMESTAMPTZ,
    step3_done_at        TIMESTAMPTZ,
    step4_done_at        TIMESTAMPTZ,
    last_error           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor                VARCHAR(128) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_saga_op_rec
    ON governance.apply_saga_operations(recommendation_id, created_at DESC);

COMMIT;
