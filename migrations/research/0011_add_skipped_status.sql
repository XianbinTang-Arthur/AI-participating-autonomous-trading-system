-- Migration 0011: Add 'skipped' to ingested_status CHECK constraint
-- Phase 1: Allows backfill to explicitly mark files as skipped
-- (e.g. candle files missing timeframe, unknown domain)

ALTER TABLE meta.raw_source_files
    DROP CONSTRAINT raw_source_files_ingested_status_check;

ALTER TABLE meta.raw_source_files
    ADD CONSTRAINT raw_source_files_ingested_status_check
    CHECK (ingested_status IN ('pending', 'ingested', 'failed', 'skipped'));
