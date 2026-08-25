-- Batch B · Stage 15 rollback — remove recommendation source-round contract

BEGIN;

DROP INDEX IF EXISTS governance.uq_rec_round_family_tf_active;

ALTER TABLE governance.recommendations
    DROP COLUMN IF EXISTS source_round_id;

COMMIT;
