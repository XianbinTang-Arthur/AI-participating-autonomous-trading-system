-- Batch B · Stage 18 — RDP historical data provenance and lifecycle ledgers.
-- Additive only: existing raw and derived facts are not rewritten.

BEGIN;

ALTER TABLE staging.raw_liquidations
    ADD COLUMN IF NOT EXISTS raw_payload_hash VARCHAR(64);
ALTER TABLE staging.raw_liquidations
    DROP CONSTRAINT IF EXISTS chk_raw_liq_payload_hash;
ALTER TABLE staging.raw_liquidations
    ADD CONSTRAINT chk_raw_liq_payload_hash CHECK (
        raw_payload_hash IS NULL OR raw_payload_hash ~ '^[0-9a-f]{64}$'
    );

CREATE TABLE IF NOT EXISTS meta.data_source_registry (
    source_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key TEXT NOT NULL CONSTRAINT uq_data_source_registry_key UNIQUE,
    source_kind TEXT NOT NULL CONSTRAINT chk_data_source_registry_kind CHECK (
        source_kind IN ('aats_ws_capture','okx_rest','okx_bulk','third_party','derived','proxy')
    ),
    provider TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    timestamp_semantics TEXT NOT NULL,
    truth_tier TEXT NOT NULL CONSTRAINT chk_data_source_registry_truth_tier CHECK (
        truth_tier IN ('authoritative_external','local_observation','derived','proxy','external_unverified')
    ),
    license_usage_note TEXT NOT NULL,
    source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_data_source_registry_kind_truth CHECK (
        (source_kind = 'aats_ws_capture' AND truth_tier = 'local_observation') OR
        (source_kind IN ('okx_rest','okx_bulk') AND truth_tier = 'authoritative_external') OR
        (source_kind = 'third_party' AND truth_tier = 'external_unverified') OR
        (source_kind = 'derived' AND truth_tier = 'derived') OR
        (source_kind = 'proxy' AND truth_tier = 'proxy')
    )
);
CREATE INDEX IF NOT EXISTS idx_data_source_registry_kind_provider
    ON meta.data_source_registry (source_kind, provider);

CREATE TABLE IF NOT EXISTS meta.archive_partitions (
    partition_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES meta.data_source_registry(source_id),
    dataset_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    coverage_start TIMESTAMPTZ NOT NULL,
    coverage_end TIMESTAMPTZ NOT NULL,
    storage_format TEXT NOT NULL DEFAULT 'parquet',
    storage_path TEXT NOT NULL,
    sha256 VARCHAR(64),
    row_count BIGINT NOT NULL DEFAULT 0,
    min_event_ts TIMESTAMPTZ,
    max_event_ts TIMESTAMPTZ,
    min_sequence BIGINT,
    max_sequence BIGINT,
    gap_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    manifest_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'DISCOVERED',
    verified_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_archive_partition_scope UNIQUE (
        source_id, dataset_name, symbol, coverage_start, coverage_end
    ),
    CONSTRAINT chk_archive_partition_range CHECK (coverage_end > coverage_start),
    CONSTRAINT chk_archive_partition_row_count CHECK (row_count >= 0),
    CONSTRAINT chk_archive_partition_sha CHECK (
        sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_archive_partition_format CHECK (storage_format = 'parquet'),
    CONSTRAINT chk_archive_partition_state CHECK (
        state IN ('DISCOVERED','ARCHIVING','VERIFIED','DELETE_ELIGIBLE','DELETED','FAILED')
    ),
    CONSTRAINT chk_archive_partition_state_shape CHECK (
        (state IN ('DISCOVERED','ARCHIVING') AND verified_at IS NULL
         AND deleted_at IS NULL) OR
        (state = 'FAILED' AND error_message IS NOT NULL
         AND deleted_at IS NULL) OR
        (state IN ('VERIFIED','DELETE_ELIGIBLE') AND sha256 IS NOT NULL
         AND row_count > 0 AND verified_at IS NOT NULL
         AND deleted_at IS NULL AND error_message IS NULL) OR
        (state = 'DELETED' AND sha256 IS NOT NULL AND row_count > 0
         AND verified_at IS NOT NULL AND deleted_at IS NOT NULL
         AND error_message IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_archive_partition_state
    ON meta.archive_partitions (state, coverage_end);
CREATE INDEX IF NOT EXISTS idx_archive_partition_dataset
    ON meta.archive_partitions (dataset_name, symbol, coverage_start);

CREATE TABLE IF NOT EXISTS meta.data_gap_records (
    gap_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID REFERENCES meta.data_source_registry(source_id),
    dataset_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    gap_start TIMESTAMPTZ NOT NULL,
    gap_end TIMESTAMPTZ NOT NULL,
    classification TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    reason_code TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_data_gap_scope_reason UNIQUE (
        dataset_name, symbol, channel, gap_start, gap_end, reason_code
    ),
    CONSTRAINT chk_data_gap_range CHECK (gap_end > gap_start),
    CONSTRAINT chk_data_gap_classification CHECK (
        classification IN ('deterministic_rebuild','official_backfill','third_party_candidate','prospective_only','cannot_recover')
    ),
    CONSTRAINT chk_data_gap_status CHECK (
        status IN ('OPEN','CLASSIFIED','BACKFILLED','REBUILT','AWAITING_LIVE_COLLECTION','CANNOT_RECOVER','THIRD_PARTY_ONLY')
    ),
    CONSTRAINT chk_data_gap_status_classification CHECK (
        status IN ('OPEN','CLASSIFIED')
        OR (classification = 'deterministic_rebuild' AND status = 'REBUILT')
        OR (classification = 'official_backfill' AND status = 'BACKFILLED')
        OR (classification = 'third_party_candidate' AND status = 'THIRD_PARTY_ONLY')
        OR (classification = 'prospective_only' AND status = 'AWAITING_LIVE_COLLECTION')
        OR (classification = 'cannot_recover' AND status = 'CANNOT_RECOVER')
    ),
    CONSTRAINT chk_data_gap_resolution_shape CHECK (
        (status IN ('BACKFILLED','REBUILT','CANNOT_RECOVER','THIRD_PARTY_ONLY')
            AND resolved_at IS NOT NULL)
        OR (status NOT IN ('BACKFILLED','REBUILT','CANNOT_RECOVER','THIRD_PARTY_ONLY')
            AND resolved_at IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_data_gap_status
    ON meta.data_gap_records (status, gap_start);
CREATE INDEX IF NOT EXISTS idx_data_gap_dataset
    ON meta.data_gap_records (dataset_name, symbol, gap_start);

CREATE TABLE IF NOT EXISTS meta.dataset_bundles (
    bundle_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bundle_key TEXT NOT NULL CONSTRAINT uq_dataset_bundle_key UNIQUE,
    dataset_version TEXT NOT NULL,
    purpose TEXT NOT NULL,
    eligibility_mode TEXT NOT NULL,
    component_sources JSONB NOT NULL,
    fingerprint VARCHAR(64) NOT NULL CONSTRAINT uq_dataset_bundle_fingerprint UNIQUE,
    coverage_start TIMESTAMPTZ NOT NULL,
    coverage_end TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'BUILDING',
    eligibility_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dataset_bundle_range CHECK (coverage_end > coverage_start),
    CONSTRAINT chk_dataset_bundle_fingerprint CHECK (
        fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_dataset_bundle_mode CHECK (
        eligibility_mode IN ('historical_research','live_capture')
    ),
    CONSTRAINT chk_dataset_bundle_status CHECK (
        status IN ('BUILDING','ELIGIBLE','INELIGIBLE','SUPERSEDED')
    )
);
CREATE INDEX IF NOT EXISTS idx_dataset_bundle_mode_status
    ON meta.dataset_bundles (eligibility_mode, status);

CREATE TABLE IF NOT EXISTS meta.data_rebuild_runs (
    rebuild_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_key TEXT NOT NULL CONSTRAINT uq_data_rebuild_operation UNIQUE,
    bundle_id UUID NOT NULL REFERENCES meta.dataset_bundles(bundle_id),
    transform_version TEXT NOT NULL,
    git_commit VARCHAR(64) NOT NULL,
    rebuild_scope JSONB NOT NULL,
    input_fingerprint VARCHAR(64) NOT NULL,
    output_fingerprint VARCHAR(64),
    status TEXT NOT NULL DEFAULT 'PLANNED',
    rows_read BIGINT NOT NULL DEFAULT 0,
    rows_written BIGINT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_data_rebuild_status CHECK (
        status IN ('PLANNED','RUNNING','SUCCEEDED','FAILED','CANCELLED')
    ),
    CONSTRAINT chk_data_rebuild_counts CHECK (rows_read >= 0 AND rows_written >= 0),
    CONSTRAINT chk_data_rebuild_hashes CHECK (
        input_fingerprint ~ '^[0-9a-f]{64}$'
        AND (output_fingerprint IS NULL OR output_fingerprint ~ '^[0-9a-f]{64}$')
    ),
    CONSTRAINT chk_data_rebuild_git_commit CHECK (
        git_commit ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'
    ),
    CONSTRAINT chk_data_rebuild_state_shape CHECK (
        (status = 'PLANNED' AND started_at IS NULL AND ended_at IS NULL
            AND output_fingerprint IS NULL AND error_message IS NULL)
        OR (status = 'RUNNING' AND started_at IS NOT NULL AND ended_at IS NULL
            AND output_fingerprint IS NULL AND error_message IS NULL)
        OR (status = 'SUCCEEDED' AND started_at IS NOT NULL AND ended_at IS NOT NULL
            AND output_fingerprint IS NOT NULL AND error_message IS NULL)
        OR (status = 'FAILED' AND started_at IS NOT NULL AND ended_at IS NOT NULL
            AND output_fingerprint IS NULL AND error_message IS NOT NULL)
        OR (status = 'CANCELLED' AND ended_at IS NOT NULL
            AND output_fingerprint IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_data_rebuild_status
    ON meta.data_rebuild_runs (status, created_at);

CREATE TABLE IF NOT EXISTS meta.collector_continuity_events (
    continuity_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collector TEXT NOT NULL,
    channel TEXT NOT NULL,
    symbol TEXT NOT NULL,
    connection_generation INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_ts TIMESTAMPTZ NOT NULL,
    event_key TEXT NOT NULL DEFAULT '',
    exchange_event_ts TIMESTAMPTZ,
    local_received_ts TIMESTAMPTZ,
    sample_ts TIMESTAMPTZ,
    payload_sequence BIGINT,
    ingest_run_id UUID NOT NULL REFERENCES meta.ingest_runs(ingest_run_id),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_collector_continuity_event UNIQUE (
        collector, channel, symbol, ingest_run_id, connection_generation,
        event_type, event_ts, event_key
    ),
    CONSTRAINT chk_collector_generation CHECK (connection_generation >= 0),
    CONSTRAINT chk_collector_continuity_type CHECK (
        event_type IN ('CONNECT','DISCONNECT','RECONNECT','MESSAGE','FLUSH','DROP','SHUTDOWN','CLOCK_SKEW')
    )
);
CREATE INDEX IF NOT EXISTS idx_collector_continuity_window
    ON meta.collector_continuity_events (collector, channel, symbol, event_ts);
CREATE INDEX IF NOT EXISTS idx_collector_continuity_generation
    ON meta.collector_continuity_events (collector, connection_generation);

CREATE TABLE IF NOT EXISTS staging.official_trade_history (
    source_id UUID NOT NULL REFERENCES meta.data_source_registry(source_id),
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    trade_id TEXT NOT NULL,
    px NUMERIC(20, 10) NOT NULL,
    sz NUMERIC(28, 10) NOT NULL,
    side TEXT NOT NULL,
    source_order_type TEXT,
    raw_payload JSONB NOT NULL,
    raw_partition_sha256 VARCHAR(64) NOT NULL,
    ingest_run_id UUID NOT NULL REFERENCES meta.ingest_runs(ingest_run_id),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_stg_official_trade_history PRIMARY KEY (source_id, symbol, ts, trade_id),
    CONSTRAINT chk_stg_official_trade_side CHECK (side IN ('buy','sell')),
    CONSTRAINT chk_stg_official_trade_values CHECK (px > 0 AND sz > 0),
    CONSTRAINT chk_stg_official_trade_sha CHECK (
        raw_partition_sha256 ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS idx_stg_official_trade_history_sym_ts
    ON staging.official_trade_history (symbol, ts);
CREATE INDEX IF NOT EXISTS idx_stg_official_trade_history_sha
    ON staging.official_trade_history (raw_partition_sha256);

CREATE TABLE IF NOT EXISTS staging.official_l2_history (
    id BIGSERIAL PRIMARY KEY,
    source_id UUID NOT NULL REFERENCES meta.data_source_registry(source_id),
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    sequence_id BIGINT,
    previous_sequence_id BIGINT,
    action TEXT NOT NULL,
    bids JSONB NOT NULL,
    asks JSONB NOT NULL,
    checksum TEXT,
    source_row_hash VARCHAR(64) NOT NULL,
    raw_partition_sha256 VARCHAR(64) NOT NULL,
    ingest_run_id UUID NOT NULL REFERENCES meta.ingest_runs(ingest_run_id),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_stg_official_l2_row UNIQUE (source_id, symbol, ts, source_row_hash),
    CONSTRAINT chk_stg_official_l2_action CHECK (action IN ('snapshot','update')),
    CONSTRAINT chk_stg_official_l2_row_hash CHECK (
        source_row_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_stg_official_l2_sha CHECK (
        raw_partition_sha256 ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS idx_stg_official_l2_sym_ts
    ON staging.official_l2_history (symbol, ts);
CREATE INDEX IF NOT EXISTS idx_stg_official_l2_sequence
    ON staging.official_l2_history (symbol, sequence_id);

CREATE TABLE IF NOT EXISTS bronze.historical_orderbook_bbo_1hz (
    bundle_id UUID NOT NULL REFERENCES meta.dataset_bundles(bundle_id),
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    source_state_ts TIMESTAMPTZ NOT NULL,
    staleness_ms INTEGER NOT NULL,
    bid_px NUMERIC(20, 10) NOT NULL,
    bid_sz NUMERIC(28, 10) NOT NULL,
    ask_px NUMERIC(20, 10) NOT NULL,
    ask_sz NUMERIC(28, 10) NOT NULL,
    source_label TEXT NOT NULL DEFAULT 'okx_bulk_l2_resampled',
    transform_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_brz_hist_bbo_1hz PRIMARY KEY (bundle_id, symbol, ts),
    CONSTRAINT chk_brz_hist_bbo_no_future CHECK (source_state_ts <= ts),
    CONSTRAINT chk_brz_hist_bbo_staleness CHECK (staleness_ms >= 0),
    CONSTRAINT chk_brz_hist_bbo_values CHECK (
        bid_px > 0 AND bid_sz > 0 AND ask_px > bid_px AND ask_sz > 0
    ),
    CONSTRAINT chk_brz_hist_bbo_source_label CHECK (
        source_label = 'okx_bulk_l2_resampled'
    )
);
CREATE INDEX IF NOT EXISTS idx_brz_hist_bbo_1hz_sym_ts
    ON bronze.historical_orderbook_bbo_1hz (symbol, ts);

CREATE TABLE IF NOT EXISTS bronze.historical_orderbook_books5_2hz (
    bundle_id UUID NOT NULL REFERENCES meta.dataset_bundles(bundle_id),
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    source_state_ts TIMESTAMPTZ NOT NULL,
    staleness_ms INTEGER NOT NULL,
    bids JSONB NOT NULL,
    asks JSONB NOT NULL,
    source_label TEXT NOT NULL DEFAULT 'okx_bulk_l2_resampled',
    transform_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_brz_hist_books5_2hz PRIMARY KEY (bundle_id, symbol, ts),
    CONSTRAINT chk_brz_hist_books5_no_future CHECK (source_state_ts <= ts),
    CONSTRAINT chk_brz_hist_books5_staleness CHECK (staleness_ms >= 0),
    CONSTRAINT chk_brz_hist_books5_source_label CHECK (
        source_label = 'okx_bulk_l2_resampled'
    )
);
CREATE INDEX IF NOT EXISTS idx_brz_hist_books5_2hz_sym_ts
    ON bronze.historical_orderbook_books5_2hz (symbol, ts);

CREATE TABLE IF NOT EXISTS bronze.market_mark_price_candles_15m (
    source_id UUID NOT NULL REFERENCES meta.data_source_registry(source_id),
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open NUMERIC(20, 10) NOT NULL,
    high NUMERIC(20, 10) NOT NULL,
    low NUMERIC(20, 10) NOT NULL,
    close NUMERIC(20, 10) NOT NULL,
    confirm BOOLEAN NOT NULL,
    source_label TEXT NOT NULL DEFAULT 'bar_proxy',
    raw_partition_sha256 VARCHAR(64) NOT NULL,
    ingest_run_id UUID NOT NULL REFERENCES meta.ingest_runs(ingest_run_id),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_brz_mark_proxy_15m PRIMARY KEY (source_id, symbol, ts),
    CONSTRAINT chk_brz_mark_proxy_15m_ohlc CHECK (
        open > 0 AND high > 0 AND low > 0 AND close > 0
        AND high >= open AND high >= close AND high >= low
        AND low <= open AND low <= close
    ),
    CONSTRAINT chk_brz_mark_proxy_15m_confirmed CHECK (confirm IS TRUE),
    CONSTRAINT chk_brz_mark_proxy_15m_source_label CHECK (
        source_label = 'bar_proxy'
    ),
    CONSTRAINT chk_brz_mark_proxy_15m_sha CHECK (
        raw_partition_sha256 ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS idx_brz_mark_proxy_15m_sym_ts
    ON bronze.market_mark_price_candles_15m (symbol, ts);

CREATE TABLE IF NOT EXISTS bronze.market_mark_price_candles_1h (
    source_id UUID NOT NULL REFERENCES meta.data_source_registry(source_id),
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open NUMERIC(20, 10) NOT NULL,
    high NUMERIC(20, 10) NOT NULL,
    low NUMERIC(20, 10) NOT NULL,
    close NUMERIC(20, 10) NOT NULL,
    confirm BOOLEAN NOT NULL,
    source_label TEXT NOT NULL DEFAULT 'bar_proxy',
    raw_partition_sha256 VARCHAR(64) NOT NULL,
    ingest_run_id UUID NOT NULL REFERENCES meta.ingest_runs(ingest_run_id),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_brz_mark_proxy_1h PRIMARY KEY (source_id, symbol, ts),
    CONSTRAINT chk_brz_mark_proxy_1h_ohlc CHECK (
        open > 0 AND high > 0 AND low > 0 AND close > 0
        AND high >= open AND high >= close AND high >= low
        AND low <= open AND low <= close
    ),
    CONSTRAINT chk_brz_mark_proxy_1h_confirmed CHECK (confirm IS TRUE),
    CONSTRAINT chk_brz_mark_proxy_1h_source_label CHECK (
        source_label = 'bar_proxy'
    ),
    CONSTRAINT chk_brz_mark_proxy_1h_sha CHECK (
        raw_partition_sha256 ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS idx_brz_mark_proxy_1h_sym_ts
    ON bronze.market_mark_price_candles_1h (symbol, ts);

CREATE TABLE IF NOT EXISTS silver.historical_orderbook_metrics_15m (
    bundle_id UUID NOT NULL REFERENCES meta.dataset_bundles(bundle_id),
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    bbo_samples_n INTEGER NOT NULL,
    books5_samples_n INTEGER NOT NULL,
    mid_price_mean NUMERIC(28, 12) NOT NULL,
    spread_bps_mean NUMERIC(28, 12) NOT NULL,
    top_imbalance_mean NUMERIC(28, 12) NOT NULL,
    max_staleness_ms INTEGER NOT NULL,
    source_max_ts TIMESTAMPTZ NOT NULL,
    transform_version TEXT NOT NULL,
    output_fingerprint VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_slv_hist_orderbook_metrics_15m
        PRIMARY KEY (bundle_id, symbol, ts),
    CONSTRAINT chk_slv_hist_orderbook_counts CHECK (
        bbo_samples_n > 0 AND books5_samples_n >= 0
    ),
    CONSTRAINT chk_slv_hist_orderbook_staleness CHECK (max_staleness_ms >= 0),
    CONSTRAINT chk_slv_hist_orderbook_values CHECK (
        mid_price_mean > 0 AND spread_bps_mean >= 0
        AND top_imbalance_mean BETWEEN -1 AND 1
    ),
    CONSTRAINT chk_slv_hist_orderbook_source_time CHECK (source_max_ts >= ts),
    CONSTRAINT chk_slv_hist_orderbook_fingerprint CHECK (
        output_fingerprint ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS idx_slv_hist_orderbook_metrics_sym_ts
    ON silver.historical_orderbook_metrics_15m (symbol, ts);

CREATE TABLE IF NOT EXISTS silver.historical_trade_flow_15m (
    bundle_id UUID NOT NULL REFERENCES meta.dataset_bundles(bundle_id),
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    trade_count INTEGER NOT NULL,
    buy_count INTEGER NOT NULL,
    sell_count INTEGER NOT NULL,
    total_size NUMERIC(38, 18) NOT NULL,
    buy_size NUMERIC(38, 18) NOT NULL,
    sell_size NUMERIC(38, 18) NOT NULL,
    vwap NUMERIC(28, 12) NOT NULL,
    trade_flow_imbalance NUMERIC(28, 12) NOT NULL,
    source_max_ts TIMESTAMPTZ NOT NULL,
    transform_version TEXT NOT NULL,
    output_fingerprint VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_slv_hist_trade_flow_15m PRIMARY KEY (bundle_id, symbol, ts),
    CONSTRAINT chk_slv_hist_trade_counts CHECK (
        trade_count > 0 AND buy_count >= 0 AND sell_count >= 0
        AND trade_count = buy_count + sell_count
    ),
    CONSTRAINT chk_slv_hist_trade_values CHECK (
        total_size > 0 AND buy_size >= 0 AND sell_size >= 0
        AND total_size = buy_size + sell_size AND vwap > 0
        AND trade_flow_imbalance BETWEEN -1 AND 1
    ),
    CONSTRAINT chk_slv_hist_trade_source_time CHECK (source_max_ts >= ts),
    CONSTRAINT chk_slv_hist_trade_fingerprint CHECK (
        output_fingerprint ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX IF NOT EXISTS idx_slv_hist_trade_flow_sym_ts
    ON silver.historical_trade_flow_15m (symbol, ts);

COMMIT;
