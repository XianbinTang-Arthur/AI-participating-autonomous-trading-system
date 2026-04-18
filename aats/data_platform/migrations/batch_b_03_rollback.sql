-- Batch B · Stage 03 rollback
BEGIN;
DROP INDEX IF EXISTS governance.ix_cost_calib_sym_tf;
DROP TABLE IF EXISTS governance.cost_calibration_runs;
COMMIT;
