-- AATS migration 0001
-- PostgreSQL append-only event store and durable repositories.

CREATE TABLE IF NOT EXISTS event_store (
    sequence_id BIGSERIAL PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL UNIQUE,
    schema_version VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    event_timestamp TIMESTAMPTZ NOT NULL,
    source_component VARCHAR(128) NOT NULL,
    topic VARCHAR(128) NOT NULL,
    event_key VARCHAR(128) NOT NULL,
    decision_id VARCHAR(64),
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_event_store_topic ON event_store (topic);
CREATE INDEX IF NOT EXISTS ix_event_store_key ON event_store (event_key);
CREATE INDEX IF NOT EXISTS ix_event_store_decision_id ON event_store (decision_id);
CREATE INDEX IF NOT EXISTS ix_event_store_event_timestamp ON event_store (event_timestamp);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    sequence_id BIGSERIAL PRIMARY KEY,
    snapshot_ts TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    total_equity NUMERIC(36, 18) NOT NULL,
    realized_pnl NUMERIC(36, 18) NOT NULL,
    unrealized_pnl NUMERIC(36, 18) NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS order_states (
    client_order_id VARCHAR(64) PRIMARY KEY,
    intent_id VARCHAR(64) NOT NULL UNIQUE,
    exchange_order_id VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(64) NOT NULL,
    submitted_ts TIMESTAMPTZ,
    last_update_ts TIMESTAMPTZ,
    requested_qty NUMERIC(36, 18) NOT NULL,
    filled_qty NUMERIC(36, 18) NOT NULL,
    remaining_qty NUMERIC(36, 18) NOT NULL,
    average_fill_price NUMERIC(36, 18),
    fees NUMERIC(36, 18) NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS fill_events (
    fill_id VARCHAR(64) PRIMARY KEY,
    decision_id VARCHAR(64) NOT NULL,
    intent_id VARCHAR(64) NOT NULL,
    client_order_id VARCHAR(64) NOT NULL,
    exchange_order_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    side VARCHAR(8) NOT NULL,
    fill_qty NUMERIC(36, 18) NOT NULL,
    fill_price NUMERIC(36, 18) NOT NULL,
    fee_amount NUMERIC(36, 18) NOT NULL,
    exchange_timestamp TIMESTAMPTZ NOT NULL,
    ingestion_timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_fill_events_decision_id ON fill_events (decision_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_intent_id ON fill_events (intent_id);

CREATE TABLE IF NOT EXISTS reconciliation_reports (
    reconciliation_id VARCHAR(64) PRIMARY KEY,
    as_of_ts TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    severity VARCHAR(32) NOT NULL,
    halt_required BOOLEAN NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_audit_records (
    audit_revision_id BIGSERIAL PRIMARY KEY,
    decision_id VARCHAR(64) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    decision_context_ref VARCHAR(64) NOT NULL,
    baseline_assessment_ref VARCHAR(64),
    ai_market_assessment_ref VARCHAR(64),
    ai_action_proposal_ref VARCHAR(64),
    position_target_ref VARCHAR(64),
    policy_decision_ref VARCHAR(64),
    risk_decision_ref VARCHAR(64),
    order_intent_refs JSONB NOT NULL,
    fill_event_refs JSONB NOT NULL,
    portfolio_delta_ref VARCHAR(64),
    reconciliation_refs JSONB NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_decision_audit_records_decision_id ON decision_audit_records (decision_id);
CREATE INDEX IF NOT EXISTS ix_decision_audit_records_updated_at ON decision_audit_records (updated_at);
