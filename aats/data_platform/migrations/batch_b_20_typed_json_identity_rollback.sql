BEGIN;

ALTER TABLE governance.decision_round_snapshots
    DROP COLUMN IF EXISTS typed_json_identity_sha256;
ALTER TABLE governance.research_round_snapshots
    DROP COLUMN IF EXISTS typed_json_identity_sha256;
ALTER TABLE governance.parameter_sets
    DROP COLUMN IF EXISTS typed_json_identity_sha256;

COMMIT;
