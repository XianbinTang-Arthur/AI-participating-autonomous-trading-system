-- Destructive rollback. Stop historical builders before running it.
-- Legacy Gold and all raw/Silver facts are deliberately retained.

BEGIN;

DROP TABLE IF EXISTS meta.historical_campaign_runs;
DROP TABLE IF EXISTS gold.historical_replay_bars;
DROP TABLE IF EXISTS meta.historical_research_artifacts;

COMMIT;
