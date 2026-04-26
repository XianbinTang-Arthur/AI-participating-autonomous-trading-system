-- Batch B · Stage 12 — Orderbook diff payload sidecar
--
-- Purpose:
--   Add the bronze sidecar table selected by
--   aats.data_platform.orderbook_diff_payload_contract. This table stores
--   orderbook payload and collector-sequence evidence adjacent to existing
--   sampled snapshot rows. It does not connect collector runtime writes.
--
-- Boundaries:
--   - schema-only execution science infrastructure
--   - no strategy, risk, execution, provider, symbol, venue, release, promotion,
--     tuning, or live order behavior change
--   - payload truth stays in bronze, not execution.*
--
-- Rollback: batch_b_12_orderbook_payloads_rollback.sql.

BEGIN;

CREATE SCHEMA IF NOT EXISTS bronze;

CREATE TABLE IF NOT EXISTS bronze.market_orderbook_payloads (
    storage_table            TEXT         NOT NULL DEFAULT 'bronze.market_orderbook_payloads',
    snapshot_table           TEXT         NOT NULL,
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,
    source_ts                TIMESTAMPTZ  NOT NULL,

    collector_sequence       BIGINT       NOT NULL,
    collector_sequence_scope TEXT         NOT NULL DEFAULT 'per_ingest_run_symbol_channel',

    row_checksum             TEXT         NOT NULL,
    checksum_version         TEXT         NOT NULL DEFAULT 'orderbook_row_v1',

    capture_status           TEXT         NOT NULL,
    payload_hash             TEXT,
    payload_schema_version   TEXT,
    payload_kind             TEXT,
    raw_payload              JSONB,

    exchange_sequence_id     TEXT,
    previous_payload_hash    TEXT,
    channel                  TEXT,
    capture_reason           TEXT,
    missing_evidence         JSONB,

    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT pk_brz_orderbook_payloads
        PRIMARY KEY (snapshot_table, symbol, ts, row_checksum),

    CONSTRAINT chk_brz_orderbook_payload_storage_table CHECK (
        storage_table = 'bronze.market_orderbook_payloads'
    ),
    CONSTRAINT chk_brz_orderbook_payload_snapshot_table CHECK (
        snapshot_table IN (
            'bronze.market_orderbook_bbo',
            'bronze.market_orderbook_books5'
        )
    ),
    CONSTRAINT chk_brz_orderbook_payload_collector_sequence CHECK (
        collector_sequence > 0
    ),
    CONSTRAINT chk_brz_orderbook_payload_sequence_scope CHECK (
        collector_sequence_scope = 'per_ingest_run_symbol_channel'
    ),
    CONSTRAINT chk_brz_orderbook_payload_row_checksum CHECK (
        row_checksum ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_brz_orderbook_payload_checksum_version CHECK (
        checksum_version = 'orderbook_row_v1'
    ),
    CONSTRAINT chk_brz_orderbook_payload_capture_status CHECK (
        capture_status IN (
            'snapshot_only_diff_payload_missing',
            'diff_payload_persisted',
            'diff_payload_unavailable'
        )
    ),
    CONSTRAINT chk_brz_orderbook_payload_hash CHECK (
        payload_hash IS NULL OR payload_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_brz_orderbook_payload_previous_hash CHECK (
        previous_payload_hash IS NULL
        OR previous_payload_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_brz_orderbook_payload_schema_version CHECK (
        payload_schema_version IS NULL
        OR payload_schema_version = 'orderbook_diff_payload_v1'
    ),
    CONSTRAINT chk_brz_orderbook_payload_diff_required CHECK (
        capture_status <> 'diff_payload_persisted'
        OR (
            payload_hash IS NOT NULL
            AND payload_schema_version = 'orderbook_diff_payload_v1'
            AND payload_kind IS NOT NULL
            AND raw_payload IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_brz_orderbook_payloads_snapshot
    ON bronze.market_orderbook_payloads (snapshot_table, symbol, ts);
CREATE INDEX IF NOT EXISTS idx_brz_orderbook_payloads_sequence
    ON bronze.market_orderbook_payloads (snapshot_table, symbol, collector_sequence);
CREATE INDEX IF NOT EXISTS idx_brz_orderbook_payloads_source_ts
    ON bronze.market_orderbook_payloads (snapshot_table, symbol, source_ts);

-- Collector sequence is scoped by ingest_run_id + symbol + channel. channel is
-- nullable in the contract, so the expression index normalizes NULL to ''.
CREATE UNIQUE INDEX IF NOT EXISTS ux_brz_orderbook_payloads_sequence_scope
    ON bronze.market_orderbook_payloads (
        ingest_run_id,
        symbol,
        COALESCE(channel, ''),
        collector_sequence
    );

COMMENT ON TABLE bronze.market_orderbook_payloads IS
    'Orderbook diff payload and collector-sequence sidecar for execution science. '
    'Schema-only until a later collector write task is explicitly implemented.';

COMMIT;
