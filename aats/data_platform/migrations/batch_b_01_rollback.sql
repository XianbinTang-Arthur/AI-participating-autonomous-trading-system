-- Batch B · Stage 01 rollback
-- 逆序 drop;保留 parameter_apply_history 的 scope 审计列(有数据后删会毁审计)

BEGIN;

DROP TABLE IF EXISTS governance.apply_saga_operations;
DROP TABLE IF EXISTS governance.rdp_daemon_heartbeat;
DROP TABLE IF EXISTS governance.system_config_history;
DROP TABLE IF EXISTS governance.system_config;

DROP INDEX IF EXISTS governance.uq_active_sleeve;
DROP INDEX IF EXISTS governance.uq_active_profile;
DROP INDEX IF EXISTS governance.uq_active_combo;

-- 恢复 active_parameter_sets 的 NOT NULL + 旧 CONSTRAINT
ALTER TABLE governance.active_parameter_sets
    ALTER COLUMN family SET NOT NULL,
    ALTER COLUMN timeframe SET NOT NULL;

ALTER TABLE governance.active_parameter_sets
    ADD CONSTRAINT uq_active_combo UNIQUE (family, timeframe);

-- 恢复 parameter_sets 的 NOT NULL
ALTER TABLE governance.parameter_sets
    ALTER COLUMN family SET NOT NULL,
    ALTER COLUMN timeframe SET NOT NULL;

-- Drop CHECK
ALTER TABLE governance.recommendations DROP CONSTRAINT IF EXISTS chk_rec_scope_fields;
ALTER TABLE governance.recommendations DROP CONSTRAINT IF EXISTS chk_rec_scope;
ALTER TABLE governance.parameter_sets DROP CONSTRAINT IF EXISTS chk_ps_scope_fields;
ALTER TABLE governance.parameter_apply_history DROP CONSTRAINT IF EXISTS chk_apply_history_scope_fields;

-- 注:parameter_apply_history.family/timeframe 的 NOT NULL 不自动恢复——
--     因为 profile/sleeve 行有 NULL,SET NOT NULL 会失败。
--     如需彻底清除 profile/sleeve 行并恢复约束,手动执行:
--       DELETE FROM governance.parameter_apply_history WHERE scope IN ('profile','sleeve');
--       ALTER TABLE governance.parameter_apply_history
--           ALTER COLUMN family SET NOT NULL, ALTER COLUMN timeframe SET NOT NULL;

-- Drop index
DROP INDEX IF EXISTS governance.ix_rec_scope_ref;
DROP INDEX IF EXISTS governance.ix_apply_history_scope_ref;

-- 注:parameter_apply_history / recommendations / parameter_sets / active_parameter_sets
--    的 scope 列和 scope_ref 列**不删**——保留审计数据完整性。
--    如需彻底清除,手动执行:
--      ALTER TABLE governance.recommendations DROP COLUMN scope;
--      ALTER TABLE governance.recommendations DROP COLUMN scope_ref;
--      (对 parameter_sets / active_parameter_sets / parameter_apply_history 同样)

COMMIT;
