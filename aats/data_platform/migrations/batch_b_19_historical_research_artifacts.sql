-- Batch B · Stage 19 — source-aware historical Gold and campaign ledgers.
-- Additive only: legacy Gold rows remain readable and are not rewritten.

BEGIN;

CREATE TABLE IF NOT EXISTS meta.historical_research_artifacts (
    artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_key TEXT NOT NULL
        CONSTRAINT uq_historical_research_artifact_operation UNIQUE,
    artifact_type TEXT NOT NULL DEFAULT 'gold_replay_bars',
    primary_bundle_id UUID NOT NULL REFERENCES meta.dataset_bundles(bundle_id),
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    coverage_start TIMESTAMPTZ NOT NULL,
    coverage_end TIMESTAMPTZ NOT NULL,
    input_bundles JSONB NOT NULL,
    input_fingerprint VARCHAR(64) NOT NULL,
    transform_version TEXT NOT NULL,
    git_commit VARCHAR(64) NOT NULL,
    status TEXT NOT NULL DEFAULT 'PLANNED',
    row_count BIGINT NOT NULL DEFAULT 0,
    output_fingerprint VARCHAR(64),
    quality_report JSONB,
    artifact_index JSONB,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_historical_research_artifact_type CHECK (
        artifact_type = 'gold_replay_bars'
    ),
    CONSTRAINT chk_historical_research_artifact_timeframe CHECK (
        timeframe IN ('15m','1H')
    ),
    CONSTRAINT chk_historical_research_artifact_range CHECK (
        coverage_end > coverage_start
    ),
    CONSTRAINT chk_historical_research_artifact_input_shape CHECK (
        jsonb_typeof(input_bundles) = 'array' AND jsonb_array_length(input_bundles) > 0
    ),
    CONSTRAINT chk_historical_research_artifact_hashes CHECK (
        input_fingerprint ~ '^[0-9a-f]{64}$'
        AND (output_fingerprint IS NULL OR output_fingerprint ~ '^[0-9a-f]{64}$')
    ),
    CONSTRAINT chk_historical_research_artifact_git_commit CHECK (
        git_commit ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'
    ),
    CONSTRAINT chk_historical_research_artifact_status CHECK (
        status IN ('PLANNED','RUNNING','SUCCEEDED','FAILED','CANCELLED')
    ),
    CONSTRAINT chk_historical_research_artifact_row_count CHECK (row_count >= 0),
    CONSTRAINT chk_historical_research_artifact_state_shape CHECK (
        (status = 'PLANNED' AND started_at IS NULL AND ended_at IS NULL
            AND output_fingerprint IS NULL AND quality_report IS NULL
            AND artifact_index IS NULL AND error_message IS NULL)
        OR (status = 'RUNNING' AND started_at IS NOT NULL AND ended_at IS NULL
            AND output_fingerprint IS NULL AND quality_report IS NULL
            AND artifact_index IS NULL AND error_message IS NULL)
        OR (status = 'SUCCEEDED' AND started_at IS NOT NULL AND ended_at IS NOT NULL
            AND output_fingerprint IS NOT NULL AND quality_report IS NOT NULL
            AND artifact_index IS NOT NULL AND error_message IS NULL)
        OR (status = 'FAILED' AND started_at IS NOT NULL AND ended_at IS NOT NULL
            AND output_fingerprint IS NULL AND quality_report IS NULL
            AND artifact_index IS NULL AND error_message IS NOT NULL)
        OR (status = 'CANCELLED' AND ended_at IS NOT NULL
            AND output_fingerprint IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_historical_research_artifact_scope
    ON meta.historical_research_artifacts (
        symbol, timeframe, coverage_start, coverage_end, status
    );
CREATE INDEX IF NOT EXISTS idx_historical_research_artifact_primary_bundle
    ON meta.historical_research_artifacts (primary_bundle_id);

CREATE TABLE IF NOT EXISTS gold.historical_replay_bars (
    artifact_id UUID NOT NULL
        REFERENCES meta.historical_research_artifacts(artifact_id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open NUMERIC(20, 10) NOT NULL,
    high NUMERIC(20, 10) NOT NULL,
    low NUMERIC(20, 10) NOT NULL,
    close NUMERIC(20, 10) NOT NULL,
    volume NUMERIC(28, 10),
    quote_volume NUMERIC(28, 10),
    is_closed BOOLEAN NOT NULL,
    aligned_funding_rate NUMERIC(18, 12),
    funding_source_ts TIMESTAMPTZ,
    source_candle_bundle_id UUID NOT NULL REFERENCES meta.dataset_bundles(bundle_id),
    source_funding_bundle_id UUID REFERENCES meta.dataset_bundles(bundle_id),
    source_lineage JSONB NOT NULL,
    transform_version TEXT NOT NULL,
    output_fingerprint VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_gold_historical_replay_bars
        PRIMARY KEY (artifact_id, symbol, timeframe, ts),
    CONSTRAINT chk_gold_historical_replay_timeframe CHECK (
        timeframe IN ('15m','1H')
    ),
    CONSTRAINT chk_gold_historical_replay_ohlc CHECK (
        open > 0 AND high > 0 AND low > 0 AND close > 0
        AND high >= open AND high >= close AND high >= low
        AND low <= open AND low <= close
    ),
    CONSTRAINT chk_gold_historical_replay_volume CHECK (
        (volume IS NULL OR volume >= 0)
        AND (quote_volume IS NULL OR quote_volume >= 0)
    ),
    CONSTRAINT chk_gold_historical_replay_funding_shape CHECK (
        (source_funding_bundle_id IS NULL AND aligned_funding_rate IS NULL
            AND funding_source_ts IS NULL)
        OR (source_funding_bundle_id IS NOT NULL
            AND (funding_source_ts IS NULL OR funding_source_ts <= ts))
    ),
    CONSTRAINT chk_gold_historical_replay_lineage_shape CHECK (
        jsonb_typeof(source_lineage) = 'array'
        AND jsonb_array_length(source_lineage) > 0
    ),
    CONSTRAINT chk_gold_historical_replay_fingerprint CHECK (
        output_fingerprint ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS idx_gold_historical_replay_scope
    ON gold.historical_replay_bars (symbol, timeframe, ts);
CREATE INDEX IF NOT EXISTS idx_gold_historical_replay_candle_bundle
    ON gold.historical_replay_bars (source_candle_bundle_id, symbol, timeframe, ts);

CREATE TABLE IF NOT EXISTS meta.historical_campaign_runs (
    campaign_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_key TEXT NOT NULL
        CONSTRAINT uq_historical_campaign_operation UNIQUE,
    symbol TEXT NOT NULL,
    coverage_start TIMESTAMPTZ NOT NULL,
    coverage_end TIMESTAMPTZ NOT NULL,
    requested_days INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PLANNED',
    capacity_report JSONB NOT NULL,
    manifest JSONB NOT NULL,
    checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_historical_campaign_range CHECK (coverage_end > coverage_start),
    CONSTRAINT chk_historical_campaign_days CHECK (requested_days > 0),
    CONSTRAINT chk_historical_campaign_json CHECK (
        jsonb_typeof(capacity_report) = 'object'
        AND jsonb_typeof(manifest) = 'object'
        AND jsonb_typeof(checkpoint) = 'object'
    ),
    CONSTRAINT chk_historical_campaign_status CHECK (
        status IN ('PLANNED','BLOCKED','RUNNING','SUCCEEDED','FAILED','CANCELLED')
    ),
    CONSTRAINT chk_historical_campaign_capacity_status CHECK (
        (status = 'BLOCKED' AND capacity_report @> '{"approved": false}'::jsonb)
        OR (status IN ('PLANNED','RUNNING','SUCCEEDED','FAILED')
            AND capacity_report @> '{"approved": true}'::jsonb)
        OR status = 'CANCELLED'
    ),
    CONSTRAINT chk_historical_campaign_state_shape CHECK (
        (status IN ('PLANNED','BLOCKED') AND started_at IS NULL AND ended_at IS NULL)
        OR (status = 'RUNNING' AND started_at IS NOT NULL AND ended_at IS NULL
            AND error_message IS NULL)
        OR (status = 'SUCCEEDED' AND started_at IS NOT NULL AND ended_at IS NOT NULL
            AND error_message IS NULL)
        OR (status = 'FAILED' AND started_at IS NOT NULL AND ended_at IS NOT NULL
            AND error_message IS NOT NULL)
        OR (status = 'CANCELLED' AND ended_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_historical_campaign_scope
    ON meta.historical_campaign_runs (symbol, coverage_start, coverage_end, status);

COMMIT;
