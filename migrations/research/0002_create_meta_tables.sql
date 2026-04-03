-- Migration 0002: Create meta tables
-- Phase 1: Research Data Platform
-- Tables: dataset_manifests, raw_source_files, ingest_runs, ingest_run_items,
--         ingest_checkpoints, quality_reports

-- 1. dataset_manifests
CREATE TABLE IF NOT EXISTS meta.dataset_manifests (
    dataset_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_name        TEXT NOT NULL,
    dataset_layer       TEXT NOT NULL CHECK (dataset_layer IN ('staging','bronze','silver','gold')),
    dataset_domain      TEXT NOT NULL CHECK (dataset_domain IN ('candles','funding')),
    instrument_type     TEXT NOT NULL CHECK (instrument_type IN ('spot','swap')),
    timeframe           TEXT NULL CHECK (timeframe IS NULL OR timeframe IN ('1m','5m','15m','1H')),
    symbol_scope        TEXT NOT NULL,
    dataset_version     TEXT NOT NULL,
    schema_version      TEXT NOT NULL DEFAULT 'v1',
    source_type         TEXT NOT NULL CHECK (source_type IN ('historical_file','api','derived')),
    source_dataset_ids  UUID[] NOT NULL DEFAULT '{}',
    start_ts            TIMESTAMPTZ NULL,
    end_ts              TIMESTAMPTZ NULL,
    row_count           BIGINT NULL,
    status              TEXT NOT NULL CHECK (status IN ('active','superseded','building','failed')) DEFAULT 'building',
    storage_table       TEXT NOT NULL,
    notes               TEXT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dm_layer_domain ON meta.dataset_manifests (dataset_layer, dataset_domain, instrument_type, timeframe);
CREATE INDEX IF NOT EXISTS idx_dm_version      ON meta.dataset_manifests (dataset_version);
CREATE INDEX IF NOT EXISTS idx_dm_status       ON meta.dataset_manifests (status);

-- 2. raw_source_files
CREATE TABLE IF NOT EXISTS meta.raw_source_files (
    source_file_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type         TEXT NOT NULL CHECK (source_type IN ('historical_file','api_snapshot')),
    dataset_domain      TEXT NOT NULL CHECK (dataset_domain IN ('candles','funding')),
    instrument_type     TEXT NULL CHECK (instrument_type IS NULL OR instrument_type IN ('spot','swap')),
    symbol_hint         TEXT NULL,
    timeframe_hint      TEXT NULL,
    source_granularity  TEXT NULL CHECK (source_granularity IS NULL OR source_granularity IN ('day','month')),
    source_path         TEXT NOT NULL,
    checksum            TEXT NULL,
    file_size_bytes     BIGINT NULL,
    downloaded_at       TIMESTAMPTZ NULL,
    discovered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_start_ts     TIMESTAMPTZ NULL,
    source_end_ts       TIMESTAMPTZ NULL,
    raw_row_count       BIGINT NULL,
    parse_status        TEXT NOT NULL CHECK (parse_status IN ('pending','parsed','failed')) DEFAULT 'pending',
    parse_error         TEXT NULL,
    ingested_status     TEXT NOT NULL CHECK (ingested_status IN ('pending','ingested','failed')) DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rsf_domain     ON meta.raw_source_files (dataset_domain, instrument_type, timeframe_hint);
CREATE INDEX IF NOT EXISTS idx_rsf_checksum   ON meta.raw_source_files (checksum);
CREATE INDEX IF NOT EXISTS idx_rsf_status     ON meta.raw_source_files (parse_status, ingested_status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rsf_path ON meta.raw_source_files (source_path);

-- 3. ingest_runs
CREATE TABLE IF NOT EXISTS meta.ingest_runs (
    ingest_run_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type            TEXT NOT NULL CHECK (run_type IN ('backfill','rolling','gap_repair','gold_build')),
    dataset_domain      TEXT NOT NULL CHECK (dataset_domain IN ('candles','funding')),
    instrument_type     TEXT NULL CHECK (instrument_type IS NULL OR instrument_type IN ('spot','swap')),
    symbol              TEXT NULL,
    timeframe           TEXT NULL,
    trigger_mode        TEXT NOT NULL CHECK (trigger_mode IN ('scheduler','manual','auto_gap_repair')) DEFAULT 'manual',
    status              TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','retrying','backfilling')) DEFAULT 'pending',
    started_at          TIMESTAMPTZ NULL,
    ended_at            TIMESTAMPTZ NULL,
    attempt_count       INT NOT NULL DEFAULT 1,
    checkpoint_before   JSONB NULL,
    checkpoint_after    JSONB NULL,
    error_message       TEXT NULL,
    notes               TEXT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ir_type_status  ON meta.ingest_runs (run_type, dataset_domain, status);
CREATE INDEX IF NOT EXISTS idx_ir_symbol       ON meta.ingest_runs (symbol, timeframe);
CREATE INDEX IF NOT EXISTS idx_ir_started      ON meta.ingest_runs (started_at DESC);

-- 4. ingest_run_items
CREATE TABLE IF NOT EXISTS meta.ingest_run_items (
    ingest_run_item_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingest_run_id       UUID NOT NULL REFERENCES meta.ingest_runs(ingest_run_id),
    dataset_domain      TEXT NOT NULL CHECK (dataset_domain IN ('candles','funding')),
    instrument_type     TEXT NULL CHECK (instrument_type IS NULL OR instrument_type IN ('spot','swap')),
    symbol              TEXT NULL,
    timeframe           TEXT NULL,
    window_start_ts     TIMESTAMPTZ NULL,
    window_end_ts       TIMESTAMPTZ NULL,
    source_file_id      UUID NULL REFERENCES meta.raw_source_files(source_file_id),
    status              TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed')) DEFAULT 'pending',
    raw_rows_read       BIGINT NULL,
    rows_written_staging BIGINT NULL,
    rows_written_bronze BIGINT NULL,
    rows_written_silver BIGINT NULL,
    rows_written_gold   BIGINT NULL,
    error_message       TEXT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_iri_run       ON meta.ingest_run_items (ingest_run_id);
CREATE INDEX IF NOT EXISTS idx_iri_status    ON meta.ingest_run_items (dataset_domain, symbol, timeframe, status);

-- 5. ingest_checkpoints
CREATE TABLE IF NOT EXISTS meta.ingest_checkpoints (
    checkpoint_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_domain      TEXT NOT NULL CHECK (dataset_domain IN ('candles','funding')),
    instrument_type     TEXT NOT NULL CHECK (instrument_type IN ('spot','swap')),
    symbol              TEXT NOT NULL,
    timeframe           TEXT NULL CHECK (timeframe IS NULL OR timeframe IN ('1m','5m','15m','1H')),
    last_successful_ts  TIMESTAMPTZ NULL,
    last_attempted_ts   TIMESTAMPTZ NULL,
    next_expected_ts    TIMESTAMPTZ NULL,
    backfill_completed  BOOLEAN NOT NULL DEFAULT FALSE,
    gap_detected        BOOLEAN NOT NULL DEFAULT FALSE,
    gap_start_ts        TIMESTAMPTZ NULL,
    gap_end_ts          TIMESTAMPTZ NULL,
    checkpoint_status   TEXT NOT NULL CHECK (checkpoint_status IN ('active','stale','gap_detected')) DEFAULT 'active',
    last_ingest_run_id  UUID NULL,
    notes               TEXT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_checkpoint_key ON meta.ingest_checkpoints (dataset_domain, instrument_type, symbol, timeframe);
CREATE INDEX IF NOT EXISTS idx_cp_status            ON meta.ingest_checkpoints (checkpoint_status);
CREATE INDEX IF NOT EXISTS idx_cp_symbol            ON meta.ingest_checkpoints (symbol, timeframe);

-- 6. quality_reports
CREATE TABLE IF NOT EXISTS meta.quality_reports (
    quality_report_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingest_run_id           UUID NULL REFERENCES meta.ingest_runs(ingest_run_id),
    dataset_layer           TEXT NOT NULL CHECK (dataset_layer IN ('staging','bronze','silver','gold')),
    dataset_domain          TEXT NOT NULL CHECK (dataset_domain IN ('candles','funding')),
    instrument_type         TEXT NULL CHECK (instrument_type IS NULL OR instrument_type IN ('spot','swap')),
    symbol                  TEXT NULL,
    timeframe               TEXT NULL,
    dataset_version         TEXT NOT NULL,
    window_start_ts         TIMESTAMPTZ NULL,
    window_end_ts           TIMESTAMPTZ NULL,
    total_rows              BIGINT NOT NULL DEFAULT 0,
    missing_intervals_count INT NOT NULL DEFAULT 0,
    duplicate_rows_count    INT NOT NULL DEFAULT 0,
    out_of_order_rows_count INT NOT NULL DEFAULT 0,
    invalid_price_rows_count INT NOT NULL DEFAULT 0,
    invalid_volume_rows_count INT NOT NULL DEFAULT 0,
    suspect_rows_count      INT NOT NULL DEFAULT 0,
    quality_status          TEXT NOT NULL CHECK (quality_status IN ('pass','warn','fail')) DEFAULT 'pass',
    details                 JSONB NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_qr_layer   ON meta.quality_reports (dataset_layer, dataset_domain, instrument_type, timeframe);
CREATE INDEX IF NOT EXISTS idx_qr_status  ON meta.quality_reports (quality_status);
CREATE INDEX IF NOT EXISTS idx_qr_version ON meta.quality_reports (dataset_version);
CREATE INDEX IF NOT EXISTS idx_qr_run     ON meta.quality_reports (ingest_run_id);
