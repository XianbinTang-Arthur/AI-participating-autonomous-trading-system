-- Batch B · Stage 15 — recommendation source-round schema parity
--
-- Older RDP databases predate RecommendationModel.source_round_id.  SQLAlchemy
-- create_all does not add columns or indexes to an existing table, so runtime
-- ORM validation must be preceded by this explicit, ledgered migration.

BEGIN;

ALTER TABLE governance.recommendations
    ADD COLUMN IF NOT EXISTS source_round_id VARCHAR(128);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rec_round_family_tf_active
    ON governance.recommendations (source_round_id, family, timeframe)
    WHERE source_round_id IS NOT NULL
      AND status NOT IN ('superseded', 'rejected');

COMMENT ON COLUMN governance.recommendations.source_round_id IS
    'Research or decision round that produced this recommendation';

COMMIT;
