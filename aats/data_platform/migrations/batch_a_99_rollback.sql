-- Batch A · Emergency rollback — DROP all batch-A constraints
--
-- See: docs/task/rdp_hardening_batch_a_detailed_design.md §4.11
--
-- Only run this in a genuine incident (a post-migration write is being
-- rejected by a CHECK/FK that shouldn't apply). Batch A tightens the
-- schema on purpose — reverting undoes every guarantee later layers
-- depend on.
--
-- Idempotency: DROP ... IF EXISTS for every object. Safe to re-run.

-- CHECK constraints (stage 4.4.4)
ALTER TABLE governance.parameter_sets DROP CONSTRAINT IF EXISTS ck_ps_status;
ALTER TABLE governance.recommendations DROP CONSTRAINT IF EXISTS ck_rec_status;
ALTER TABLE governance.parameter_apply_history DROP CONSTRAINT IF EXISTS ck_apply_op_type;
ALTER TABLE governance.parameter_releases DROP CONSTRAINT IF EXISTS ck_release_apply_result;
ALTER TABLE governance.parameter_releases DROP CONSTRAINT IF EXISTS ck_release_observation_status;
ALTER TABLE governance.observation_results DROP CONSTRAINT IF EXISTS ck_obs_status;
ALTER TABLE governance.observation_results DROP CONSTRAINT IF EXISTS ck_obs_recommendation;
ALTER TABLE governance.rollback_recommendations DROP CONSTRAINT IF EXISTS ck_rollback_severity;
ALTER TABLE governance.release_effectiveness DROP CONSTRAINT IF EXISTS ck_release_eff_conclusion;

-- UQ index + backfilled column (stage 4.4.3)
DROP INDEX IF EXISTS governance.uq_rec_round_family_tf_active;
ALTER TABLE governance.recommendations DROP COLUMN IF EXISTS source_round_id;

-- FOREIGN KEYs (stage 4.4.2)
ALTER TABLE governance.active_parameter_sets DROP CONSTRAINT IF EXISTS fk_active_ps_id;
ALTER TABLE governance.parameter_apply_history DROP CONSTRAINT IF EXISTS fk_apply_history_to_ps;
ALTER TABLE governance.parameter_apply_history DROP CONSTRAINT IF EXISTS fk_apply_history_from_ps;
ALTER TABLE governance.parameter_releases DROP CONSTRAINT IF EXISTS fk_param_release_ps;
ALTER TABLE governance.parameter_releases DROP CONSTRAINT IF EXISTS fk_param_release_prev_ps;
ALTER TABLE governance.rollback_recommendations DROP CONSTRAINT IF EXISTS fk_rollback_rec_target_ps;
ALTER TABLE governance.active_decisions DROP CONSTRAINT IF EXISTS fk_active_decision_ps;
