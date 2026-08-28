-- Batch B 20: preserve application JSON number-type identity across JSONB.
-- Existing rows remain nullable and are safely fingerprinted on their first
-- exact application retry.  This avoids inventing an int/float provenance for
-- historical rows while all new rows carry a durable SHA-256 anchor.

BEGIN;

ALTER TABLE governance.parameter_sets
    ADD COLUMN IF NOT EXISTS typed_json_identity_sha256 VARCHAR(64);
ALTER TABLE governance.research_round_snapshots
    ADD COLUMN IF NOT EXISTS typed_json_identity_sha256 VARCHAR(64);
ALTER TABLE governance.decision_round_snapshots
    ADD COLUMN IF NOT EXISTS typed_json_identity_sha256 VARCHAR(64);

ALTER TABLE governance.parameter_sets
    DROP CONSTRAINT IF EXISTS chk_parameter_sets_typed_json_identity_sha256;
ALTER TABLE governance.parameter_sets
    ADD CONSTRAINT chk_parameter_sets_typed_json_identity_sha256 CHECK (
        typed_json_identity_sha256 IS NULL
        OR typed_json_identity_sha256 ~ '^[0-9a-f]{64}$'
    );

ALTER TABLE governance.research_round_snapshots
    DROP CONSTRAINT IF EXISTS chk_research_rounds_typed_json_identity_sha256;
ALTER TABLE governance.research_round_snapshots
    ADD CONSTRAINT chk_research_rounds_typed_json_identity_sha256 CHECK (
        typed_json_identity_sha256 IS NULL
        OR typed_json_identity_sha256 ~ '^[0-9a-f]{64}$'
    );

ALTER TABLE governance.decision_round_snapshots
    DROP CONSTRAINT IF EXISTS chk_decision_rounds_typed_json_identity_sha256;
ALTER TABLE governance.decision_round_snapshots
    ADD CONSTRAINT chk_decision_rounds_typed_json_identity_sha256 CHECK (
        typed_json_identity_sha256 IS NULL
        OR typed_json_identity_sha256 ~ '^[0-9a-f]{64}$'
    );

COMMIT;
