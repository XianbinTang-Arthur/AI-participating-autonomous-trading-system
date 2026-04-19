-- Batch A · Stage 4.4.4 — Add CHECK constraints on status-like columns
--
-- See: docs/task/rdp_hardening_batch_a_detailed_design.md §4.8
--
-- All allowlists are calibrated against the actual writers in the codebase
-- (evidenced by stage 4.4.1 distribution checks in _batch_a.py). Any value
-- outside the allowlist was already reported as illegal during dry-run and
-- must have been cleaned up before this stage runs.
--
-- Idempotency: DO $$ EXCEPTION blocks. Re-running the migration is a no-op.

-- parameter_sets.status
-- Bug 9 修复 (2026-04-19): allowlist 加入 'released'。
-- apply 事务把 target parameter_set 从 candidate 升 released，保持每 combo
-- 任一时刻最多 1 条 released 的 invariant。旧的 4 值 allowlist 在新 DB 上
-- 跑 apply 会触发 CHECK violation。
DO $$ BEGIN
  ALTER TABLE governance.parameter_sets
    ADD CONSTRAINT ck_ps_status
    CHECK (status IN ('draft', 'candidate', 'frozen', 'released', 'deprecated'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- recommendations.status
DO $$ BEGIN
  ALTER TABLE governance.recommendations
    ADD CONSTRAINT ck_rec_status
    CHECK (status IN ('draft', 'approved', 'rejected', 'superseded'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- parameter_apply_history.operation_type
DO $$ BEGIN
  ALTER TABLE governance.parameter_apply_history
    ADD CONSTRAINT ck_apply_op_type
    CHECK (operation_type IN ('apply', 'rollback', 'clear'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- parameter_releases.apply_result
DO $$ BEGIN
  ALTER TABLE governance.parameter_releases
    ADD CONSTRAINT ck_release_apply_result
    CHECK (apply_result IN ('pending', 'blocked_by_gate', 'success', 'failed'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- parameter_releases.observation_status
DO $$ BEGIN
  ALTER TABLE governance.parameter_releases
    ADD CONSTRAINT ck_release_observation_status
    CHECK (observation_status IN (
      'pending', 'observing', 'completed', 'rollback_recommended', 'rolled_back'
    ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- observation_results.status
DO $$ BEGIN
  ALTER TABLE governance.observation_results
    ADD CONSTRAINT ck_obs_status
    CHECK (status IN ('observing', 'completed', 'rollback_recommended'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- observation_results.recommendation
DO $$ BEGIN
  ALTER TABLE governance.observation_results
    ADD CONSTRAINT ck_obs_recommendation
    CHECK (recommendation IN ('keep', 'review', 'rollback_recommended'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- rollback_recommendations.severity
DO $$ BEGIN
  ALTER TABLE governance.rollback_recommendations
    ADD CONSTRAINT ck_rollback_severity
    CHECK (severity IN ('none', 'medium', 'high'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- release_effectiveness.conclusion
DO $$ BEGIN
  ALTER TABLE governance.release_effectiveness
    ADD CONSTRAINT ck_release_eff_conclusion
    CHECK (conclusion IN (
      'rollback_triggered', 'insufficient_evidence', 'ineffective', 'effective', 'mixed'
    ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
