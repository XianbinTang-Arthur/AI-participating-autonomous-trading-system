-- Batch B · Stage 04 — Phase 3 sleeve advice view
-- 方案 A(R1-17):sleeve advice 不建独立表,统一走 recommendations(scope='sleeve')
-- 本 migration 只建查询视图,给 UI 一个便捷入口。
-- 回滚: batch_b_04_rollback.sql

BEGIN;

CREATE OR REPLACE VIEW governance.vw_sleeve_advice_recent AS
SELECT
    r.recommendation_id,
    r.scope_ref AS sleeve_id,
    r.recommendation_type,
    r.evidence_bundle_ref,
    r.reason,
    r.status,
    r.created_at,
    ps.values AS proposed
FROM governance.recommendations r
LEFT JOIN governance.parameter_sets ps
  ON r.target_parameter_set_id = ps.parameter_set_id
WHERE r.scope = 'sleeve';

COMMIT;
