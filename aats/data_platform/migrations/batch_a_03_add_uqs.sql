-- Batch A · Stage 4.4.3 — Add source_round_id column + partial unique index
--
-- See: docs/task/rdp_hardening_batch_a_detailed_design.md §4.7
--
-- Constraint target: "one non-superseded/non-rejected recommendation per
-- (source_round_id, family, timeframe)". This prevents the bug where two
-- approvers race to promote two different parameter candidates from the
-- same research round.
--
-- Why a new column instead of reading through target_parameter_set_id →
-- parameter_sets.source_round_id? The indirection makes the UQ impossible
-- at the DB level — you can't CREATE UNIQUE INDEX on a JOIN. Keeping it
-- nullable lets historical rows stay uncovered until backfilled.
--
-- Idempotency: ADD COLUMN IF NOT EXISTS, UPDATE bounded by WHERE
-- source_round_id IS NULL, CREATE UNIQUE INDEX IF NOT EXISTS.

ALTER TABLE governance.recommendations
  ADD COLUMN IF NOT EXISTS source_round_id VARCHAR(128);

-- Backfill source_round_id from the parameter_set the recommendation targets.
-- Scoped to still-NULL rows so re-running never clobbers operator-set values.
UPDATE governance.recommendations r
SET source_round_id = p.source_round_id
FROM governance.parameter_sets p
WHERE r.target_parameter_set_id = p.parameter_set_id
  AND r.source_round_id IS NULL
  AND p.source_round_id IS NOT NULL;

-- Partial unique index: only enforced when source_round_id is set AND the
-- recommendation is still live (neither superseded nor rejected). superseded
-- rows are the tail of a normal retry flow; rejected rows are dead ends.
-- Both must be re-issuable without tripping the UQ.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rec_round_family_tf_active
  ON governance.recommendations (source_round_id, family, timeframe)
  WHERE source_round_id IS NOT NULL
    AND status NOT IN ('superseded', 'rejected');
