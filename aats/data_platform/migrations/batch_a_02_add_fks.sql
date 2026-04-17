-- Batch A · Stage 4.4.2 — Add FOREIGN KEY constraints
--
-- See: docs/task/rdp_hardening_batch_a_detailed_design.md §4.6
--
-- All seven FKs reference governance.parameter_sets(parameter_set_id) and use
-- ON DELETE RESTRICT / ON UPDATE RESTRICT. parameter_sets are retired via the
-- 'deprecated' status, never hard-deleted, so RESTRICT is the correct guard.
--
-- Idempotency: each ADD CONSTRAINT is wrapped in a DO $$ EXCEPTION block so
-- re-running the migration is a no-op. The outer transaction is provided by
-- the caller (db.apply_batch_a_migrations uses engine.begin()).
--
-- Pre-req: batch_a_01_orphan_report.sql must have returned CLEAN — a dirty
-- report means rows that would be rejected by these FKs and the ADD would
-- fail outright (no EXCEPTION handler can recover from a validation error).

-- FK 1 · active_parameter_sets.parameter_set_id → parameter_sets
DO $$ BEGIN
  ALTER TABLE governance.active_parameter_sets
    ADD CONSTRAINT fk_active_ps_id
    FOREIGN KEY (parameter_set_id)
    REFERENCES governance.parameter_sets(parameter_set_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- FK 2 · parameter_apply_history.to_parameter_set_id → parameter_sets
DO $$ BEGIN
  ALTER TABLE governance.parameter_apply_history
    ADD CONSTRAINT fk_apply_history_to_ps
    FOREIGN KEY (to_parameter_set_id)
    REFERENCES governance.parameter_sets(parameter_set_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- FK 3 · parameter_apply_history.from_parameter_set_id → parameter_sets
DO $$ BEGIN
  ALTER TABLE governance.parameter_apply_history
    ADD CONSTRAINT fk_apply_history_from_ps
    FOREIGN KEY (from_parameter_set_id)
    REFERENCES governance.parameter_sets(parameter_set_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- FK 4 · parameter_releases.parameter_set_id → parameter_sets
DO $$ BEGIN
  ALTER TABLE governance.parameter_releases
    ADD CONSTRAINT fk_param_release_ps
    FOREIGN KEY (parameter_set_id)
    REFERENCES governance.parameter_sets(parameter_set_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- FK 5 · parameter_releases.previous_parameter_set_id → parameter_sets
DO $$ BEGIN
  ALTER TABLE governance.parameter_releases
    ADD CONSTRAINT fk_param_release_prev_ps
    FOREIGN KEY (previous_parameter_set_id)
    REFERENCES governance.parameter_sets(parameter_set_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- FK 6 · rollback_recommendations.suggested_target_parameter_set_id → parameter_sets
DO $$ BEGIN
  ALTER TABLE governance.rollback_recommendations
    ADD CONSTRAINT fk_rollback_rec_target_ps
    FOREIGN KEY (suggested_target_parameter_set_id)
    REFERENCES governance.parameter_sets(parameter_set_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- FK 7 · active_decisions.active_parameter_set_id → parameter_sets
DO $$ BEGIN
  ALTER TABLE governance.active_decisions
    ADD CONSTRAINT fk_active_decision_ps
    FOREIGN KEY (active_parameter_set_id)
    REFERENCES governance.parameter_sets(parameter_set_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
