-- =============================================================================
-- RDP Batch A Migration — Stage 4.4.1: Orphan / Distribution Report
-- =============================================================================
-- PURPOSE:
--   Pre-flight scan that MUST pass before adding FOREIGN KEY / UNIQUE / CHECK
--   constraints in stages 4.4.2 - 4.4.4.
--
-- CATEGORY:
--   1-6  ORPHAN checks — MUST return ZERO rows. Any row blocks the migration.
--   7    DISTRIBUTION check — informational. Operator reviews; illegal values
--        (i.e. values NOT in the CHECK-constraint allowlist) block the migration.
--
-- HOW TO RUN (standalone):
--   docker exec -i aats-postgres psql -U admin -d aats_live_derivatives \
--       < aats/data_platform/migrations/batch_a_01_orphan_report.sql
--
-- HOW TO RUN (programmatic + structured report):
--   .venv\Scripts\python.exe scripts/rdp_run_batch_a_migration.py --dry-run
--
-- GUARANTEE:
--   Purely read-only — no DDL, no DML. Safe to run on production.
-- =============================================================================

\echo ''
\echo '########################################################################'
\echo '# RDP Batch A — Orphan / Distribution Report (READ-ONLY)'
\echo '########################################################################'

\echo ''
\echo '=== 1. active_parameter_sets.parameter_set_id -> parameter_sets ==='
\echo '    Expected: 0 rows'
SELECT a.family, a.timeframe, a.parameter_set_id
FROM governance.active_parameter_sets a
LEFT JOIN governance.parameter_sets p
  ON a.parameter_set_id = p.parameter_set_id
WHERE p.parameter_set_id IS NULL;

\echo ''
\echo '=== 2. parameter_apply_history.to_parameter_set_id -> parameter_sets ==='
\echo '    Expected: 0 rows'
SELECT h.operation_id, h.family, h.timeframe, h.to_parameter_set_id
FROM governance.parameter_apply_history h
LEFT JOIN governance.parameter_sets p
  ON h.to_parameter_set_id = p.parameter_set_id
WHERE h.to_parameter_set_id IS NOT NULL
  AND p.parameter_set_id IS NULL;

\echo ''
\echo '=== 3. parameter_apply_history.from_parameter_set_id -> parameter_sets ==='
\echo '    Expected: 0 rows'
SELECT h.operation_id, h.family, h.timeframe, h.from_parameter_set_id
FROM governance.parameter_apply_history h
LEFT JOIN governance.parameter_sets p
  ON h.from_parameter_set_id = p.parameter_set_id
WHERE h.from_parameter_set_id IS NOT NULL
  AND p.parameter_set_id IS NULL;

\echo ''
\echo '=== 4. parameter_releases.parameter_set_id + previous_parameter_set_id ==='
\echo '    Expected: 0 rows'
SELECT r.release_id, r.parameter_set_id AS missing_current_ps
FROM governance.parameter_releases r
LEFT JOIN governance.parameter_sets p
  ON r.parameter_set_id = p.parameter_set_id
WHERE p.parameter_set_id IS NULL
UNION ALL
SELECT r.release_id, r.previous_parameter_set_id AS missing_previous_ps
FROM governance.parameter_releases r
LEFT JOIN governance.parameter_sets p
  ON r.previous_parameter_set_id = p.parameter_set_id
WHERE r.previous_parameter_set_id IS NOT NULL
  AND p.parameter_set_id IS NULL;

\echo ''
\echo '=== 5. rollback_recommendations.suggested_target_parameter_set_id ==='
\echo '    Expected: 0 rows'
SELECT r.release_id, r.suggested_target_parameter_set_id
FROM governance.rollback_recommendations r
LEFT JOIN governance.parameter_sets p
  ON r.suggested_target_parameter_set_id = p.parameter_set_id
WHERE r.suggested_target_parameter_set_id IS NOT NULL
  AND p.parameter_set_id IS NULL;

\echo ''
\echo '=== 6. active_decisions.active_parameter_set_id -> parameter_sets ==='
\echo '    Expected: 0 rows'
SELECT d.family, d.timeframe, d.active_parameter_set_id
FROM governance.active_decisions d
LEFT JOIN governance.parameter_sets p
  ON d.active_parameter_set_id = p.parameter_set_id
WHERE d.active_parameter_set_id IS NOT NULL
  AND p.parameter_set_id IS NULL;

\echo ''
\echo '=== 7a. parameter_sets.status distribution ==='
\echo '    Allowed after batch A: (draft, candidate, frozen, deprecated)'
\echo '    Source of truth: VALID_PS_STATUSES in governance/_db_util.py'
SELECT status, COUNT(*) AS rows
FROM governance.parameter_sets
GROUP BY status
ORDER BY status;

\echo ''
\echo '=== 7b. recommendations.status distribution ==='
\echo '    Allowed after batch A: (draft, approved, rejected, superseded)'
\echo '    Source of truth: VALID_REC_STATUSES in governance/_db_util.py'
SELECT status, COUNT(*) AS rows
FROM governance.recommendations
GROUP BY status
ORDER BY status;

\echo ''
\echo '=== 7c. parameter_apply_history.operation_type distribution ==='
\echo '    Allowed after batch A: (apply, rollback, clear)'
\echo '    Writers: decision_system/active_parameter_apply.py'
SELECT operation_type, COUNT(*) AS rows
FROM governance.parameter_apply_history
GROUP BY operation_type
ORDER BY operation_type;

\echo ''
\echo '=== 7d. parameter_releases.apply_result distribution ==='
\echo '    Allowed after batch A: (pending, blocked_by_gate, success, failed)'
\echo '    Writers: production_workflow/release_registry.py'
SELECT apply_result, COUNT(*) AS rows
FROM governance.parameter_releases
GROUP BY apply_result
ORDER BY apply_result;

\echo ''
\echo '=== 7e. parameter_releases.observation_status distribution ==='
\echo '    Allowed: (pending, observing, completed, rollback_recommended, rolled_back)'
\echo '    Writers: release_registry.py + production_workflow/observation_window.py'
SELECT observation_status, COUNT(*) AS rows
FROM governance.parameter_releases
GROUP BY observation_status
ORDER BY observation_status;

\echo ''
\echo '=== 7f. observation_results.status + recommendation distribution ==='
\echo '    Allowed status: (observing, completed, rollback_recommended)'
\echo '    Allowed recommendation: (keep, review, rollback_recommended)'
\echo '    Writers: production_workflow/observation_window.py'
SELECT 'status' AS field, status AS value, COUNT(*) AS rows
FROM governance.observation_results
GROUP BY status
UNION ALL
SELECT 'recommendation', recommendation, COUNT(*)
FROM governance.observation_results
GROUP BY recommendation
ORDER BY field, value;

\echo ''
\echo '=== 7g. rollback_recommendations.severity distribution ==='
\echo '    Allowed after batch A: (none, medium, high)'
\echo '    Writers: production_workflow/rollback_policy.py (column default none)'
SELECT severity, COUNT(*) AS rows
FROM governance.rollback_recommendations
GROUP BY severity
ORDER BY severity;

\echo ''
\echo '=== 7h. release_effectiveness.conclusion distribution ==='
\echo '    Allowed after batch A: (rollback_triggered, insufficient_evidence, ineffective, effective, mixed)'
\echo '    Writers: metrics/release_effectiveness.py::_derive_effectiveness'
SELECT conclusion, COUNT(*) AS rows
FROM governance.release_effectiveness
GROUP BY conclusion
ORDER BY conclusion;

\echo ''
\echo '=== 8. recommendations.source_round_id backfill feasibility ==='
\echo '    How many rows will have source_round_id populated via join?'
SELECT
  SUM(CASE WHEN p.source_round_id IS NOT NULL THEN 1 ELSE 0 END) AS will_populate,
  SUM(CASE WHEN p.source_round_id IS NULL THEN 1 ELSE 0 END) AS will_stay_null,
  COUNT(*) AS total_recommendations
FROM governance.recommendations r
LEFT JOIN governance.parameter_sets p
  ON r.target_parameter_set_id = p.parameter_set_id;

\echo ''
\echo '########################################################################'
\echo '# Report finished. If any of 1-6 returned rows OR any distribution in 7'
\echo '# shows disallowed values, STOP HERE and triage before running stage 2.'
\echo '########################################################################'
