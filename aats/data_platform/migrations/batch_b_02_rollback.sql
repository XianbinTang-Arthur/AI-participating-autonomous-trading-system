-- Batch B · Stage 02 rollback
BEGIN;
DROP INDEX IF EXISTS governance.ix_profile_research_profile_started;
DROP TABLE IF EXISTS governance.profile_type_review_streak;
DROP TABLE IF EXISTS governance.profile_research_runs;
COMMIT;
