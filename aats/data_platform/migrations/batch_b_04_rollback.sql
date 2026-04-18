-- Batch B · Stage 04 rollback
BEGIN;
DROP VIEW IF EXISTS governance.vw_sleeve_advice_recent;
COMMIT;
