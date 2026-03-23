-- AATS PostgreSQL baseline schema
-- This file intentionally replaces the legacy incremental migration chain.
-- It defines the latest supported schema for fresh PostgreSQL deployments.

CREATE TABLE IF NOT EXISTS command_outbox (
    event_id VARCHAR(64) PRIMARY KEY,
    aggregate_type VARCHAR(32) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,
    topic VARCHAR(128) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempt_count INTEGER NOT NULL,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_command_outbox_status_created ON command_outbox (status, created_at);
CREATE INDEX IF NOT EXISTS ix_command_outbox_topic ON command_outbox (topic);
CREATE INDEX IF NOT EXISTS ix_command_outbox_status ON command_outbox (status);
CREATE INDEX IF NOT EXISTS ix_command_outbox_aggregate_type ON command_outbox (aggregate_type);
CREATE INDEX IF NOT EXISTS ix_command_outbox_aggregate_id ON command_outbox (aggregate_id);

CREATE TABLE IF NOT EXISTS decision_audit_records (
    audit_revision_id SERIAL PRIMARY KEY,
    decision_id VARCHAR(64) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    decision_context_ref VARCHAR(64) NOT NULL,
    baseline_assessment_ref VARCHAR(64),
    ai_market_assessment_ref VARCHAR(64),
    ai_action_proposal_ref VARCHAR(64),
    position_target_ref VARCHAR(64),
    policy_decision_ref VARCHAR(64),
    risk_decision_ref VARCHAR(64),
    execution_plan_ref VARCHAR(64),
    order_intent_refs JSONB NOT NULL,
    order_state_refs JSONB NOT NULL,
    fill_event_refs JSONB NOT NULL,
    portfolio_delta_ref VARCHAR(64),
    reconciliation_refs JSONB NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_audit_records_decision_id ON decision_audit_records (decision_id);
CREATE INDEX IF NOT EXISTS ix_decision_audit_records_updated_at ON decision_audit_records (updated_at);

CREATE TABLE IF NOT EXISTS event_store (
    sequence_id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL,
    schema_version VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    event_timestamp TIMESTAMPTZ NOT NULL,
    source_component VARCHAR(128) NOT NULL,
    topic VARCHAR(128) NOT NULL,
    event_key VARCHAR(128) NOT NULL,
    decision_id VARCHAR(64),
    symbol VARCHAR(64),
    timeframe VARCHAR(16),
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    payload JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_event_store_event_id ON event_store (event_id);
CREATE INDEX IF NOT EXISTS ix_event_store_event_key ON event_store (event_key);
CREATE INDEX IF NOT EXISTS ix_event_store_topic ON event_store (topic);
CREATE INDEX IF NOT EXISTS ix_event_store_event_type ON event_store (event_type);
CREATE INDEX IF NOT EXISTS ix_event_store_event_timestamp ON event_store (event_timestamp);
CREATE INDEX IF NOT EXISTS ix_event_store_decision_id ON event_store (decision_id);

CREATE TABLE IF NOT EXISTS execution_orders (
    order_id VARCHAR(64) PRIMARY KEY,
    intent_id VARCHAR(64) NOT NULL,
    decision_id VARCHAR(64),
    client_order_id VARCHAR(64),
    venue_order_id VARCHAR(64),
    symbol VARCHAR(64) NOT NULL,
    side VARCHAR(8) NOT NULL,
    order_type VARCHAR(16) NOT NULL,
    time_in_force VARCHAR(16),
    requested_qty NUMERIC(36, 18) NOT NULL,
    limit_price NUMERIC(36, 18),
    reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    close_only BOOLEAN NOT NULL DEFAULT FALSE,
    td_mode VARCHAR(16),
    position_mode VARCHAR(32),
    pos_side VARCHAR(16),
    reduce_only_reason VARCHAR(64),
    close_only_reason VARCHAR(64),
    instrument_family VARCHAR(64),
    settle_currency VARCHAR(16),
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    execution_action VARCHAR(32),
    position_intent VARCHAR(32),
    state VARCHAR(32) NOT NULL,
    state_version INTEGER NOT NULL,
    source_system VARCHAR(32) NOT NULL,
    last_exchange_ts TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    raw_payload JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_orders_intent_id ON execution_orders (intent_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_orders_client_order_id ON execution_orders (client_order_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_orders_venue_order_id ON execution_orders (venue_order_id);
CREATE INDEX IF NOT EXISTS ix_execution_orders_symbol ON execution_orders (symbol);
CREATE INDEX IF NOT EXISTS ix_execution_orders_state ON execution_orders (state);
CREATE INDEX IF NOT EXISTS ix_execution_orders_decision_id ON execution_orders (decision_id);
CREATE INDEX IF NOT EXISTS ix_execution_orders_product_type ON execution_orders (product_type);
CREATE INDEX IF NOT EXISTS ix_execution_orders_margin_mode ON execution_orders (margin_mode);
CREATE INDEX IF NOT EXISTS ix_execution_orders_position_intent ON execution_orders (position_intent);
CREATE INDEX IF NOT EXISTS ix_execution_orders_symbol_state ON execution_orders (symbol, state);
CREATE INDEX IF NOT EXISTS ix_execution_orders_decision_created ON execution_orders (decision_id, created_at);

CREATE TABLE IF NOT EXISTS external_event_inbox (
    inbox_id VARCHAR(64) PRIMARY KEY,
    source_system VARCHAR(32) NOT NULL,
    dedupe_key VARCHAR(256) NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    processing_result VARCHAR(16),
    last_error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_external_event_inbox_dedupe_key ON external_event_inbox (dedupe_key);
CREATE INDEX IF NOT EXISTS ix_external_event_inbox_source_system ON external_event_inbox (source_system);
CREATE INDEX IF NOT EXISTS ix_external_event_inbox_processing_result ON external_event_inbox (processing_result);
CREATE INDEX IF NOT EXISTS ix_external_event_inbox_source_received ON external_event_inbox (source_system, received_at);

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
    reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    close_only BOOLEAN NOT NULL DEFAULT FALSE,
    td_mode VARCHAR(16),
    position_mode VARCHAR(32),
    pos_side VARCHAR(16),
    reduce_only_reason VARCHAR(64),
    close_only_reason VARCHAR(64),
    instrument_family VARCHAR(64),
    settle_currency VARCHAR(16),
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    position_intent VARCHAR(32),
    exchange_timestamp TIMESTAMPTZ NOT NULL,
    ingestion_timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fill_events_decision_id ON fill_events (decision_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_intent_id ON fill_events (intent_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_client_order_id ON fill_events (client_order_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_symbol ON fill_events (symbol);

CREATE TABLE IF NOT EXISTS fill_outcomes (
    fill_id VARCHAR(64) PRIMARY KEY,
    decision_id VARCHAR(64),
    intent_id VARCHAR(64),
    order_id VARCHAR(64),
    symbol VARCHAR(32) NOT NULL,
    venue VARCHAR(16),
    side VARCHAR(8),
    fill_qty NUMERIC(36, 18),
    fill_price NUMERIC(36, 18),
    fill_notional NUMERIC(36, 18),
    fee_amount NUMERIC(36, 18),
    fee_currency VARCHAR(16),
    liquidity_role VARCHAR(16),
    exchange_timestamp TIMESTAMPTZ,
    ingestion_timestamp TIMESTAMPTZ,
    order_status_after_fill VARCHAR(32),
    target_leverage DOUBLE PRECISION,
    exposure_side VARCHAR(16),
    execution_action VARCHAR(16),
    position_intent VARCHAR(32),
    starting_position_qty NUMERIC(36, 18),
    starting_avg_entry_price NUMERIC(36, 18),
    ending_position_qty NUMERIC(36, 18),
    ending_avg_entry_price NUMERIC(36, 18),
    realized_pnl_delta NUMERIC(36, 18) NOT NULL,
    fee_delta NUMERIC(36, 18) NOT NULL,
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_decision_id ON fill_outcomes (decision_id);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_intent_id ON fill_outcomes (intent_id);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_order_id ON fill_outcomes (order_id, created_at);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_symbol ON fill_outcomes (symbol);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_side ON fill_outcomes (side);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_created_at ON fill_outcomes (created_at);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_ingestion_timestamp ON fill_outcomes (ingestion_timestamp);

CREATE TABLE IF NOT EXISTS ledger_accounts (
    account_id VARCHAR(64) PRIMARY KEY,
    account_type VARCHAR(32) NOT NULL,
    currency VARCHAR(16) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    symbol VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_accounts_identity
    ON ledger_accounts (account_type, currency, product_type, margin_mode, symbol);
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_account_type ON ledger_accounts (account_type);
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_currency ON ledger_accounts (currency);
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_product_type ON ledger_accounts (product_type);
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_margin_mode ON ledger_accounts (margin_mode);
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_symbol ON ledger_accounts (symbol);

CREATE TABLE IF NOT EXISTS ledger_journals (
    journal_id VARCHAR(64) PRIMARY KEY,
    journal_type VARCHAR(32) NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    posted_at TIMESTAMPTZ,
    metadata JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_journals_source ON ledger_journals (source_type, source_id);
CREATE INDEX IF NOT EXISTS ix_ledger_journals_source_type ON ledger_journals (source_type);
CREATE INDEX IF NOT EXISTS ix_ledger_journals_source_id ON ledger_journals (source_id);
CREATE INDEX IF NOT EXISTS ix_ledger_journals_status ON ledger_journals (status);
CREATE INDEX IF NOT EXISTS ix_ledger_journals_status_created ON ledger_journals (status, created_at);

CREATE TABLE IF NOT EXISTS operator_users (
    user_id VARCHAR(64) PRIMARY KEY,
    username VARCHAR(128) NOT NULL,
    password_hash VARCHAR(512) NOT NULL,
    role VARCHAR(16) NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    last_login_at TIMESTAMPTZ,
    payload JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_operator_users_username ON operator_users (username);
CREATE INDEX IF NOT EXISTS ix_operator_users_role ON operator_users (role);

CREATE TABLE IF NOT EXISTS order_obligations (
    client_order_id VARCHAR(64) PRIMARY KEY,
    obligation_id VARCHAR(64) NOT NULL,
    decision_id VARCHAR(64) NOT NULL,
    intent_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    reserve_currency VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL,
    reserved_amount NUMERIC(36, 18) NOT NULL,
    consumed_amount NUMERIC(36, 18) NOT NULL,
    released_amount NUMERIC(36, 18) NOT NULL,
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    last_update_ts TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_order_obligations_obligation_id ON order_obligations (obligation_id);
CREATE INDEX IF NOT EXISTS ix_order_obligations_decision_id ON order_obligations (decision_id);
CREATE INDEX IF NOT EXISTS ix_order_obligations_intent_id ON order_obligations (intent_id);
CREATE INDEX IF NOT EXISTS ix_order_obligations_symbol ON order_obligations (symbol);
CREATE INDEX IF NOT EXISTS ix_order_obligations_reserve_currency ON order_obligations (reserve_currency);
CREATE INDEX IF NOT EXISTS ix_order_obligations_status ON order_obligations (status);
CREATE INDEX IF NOT EXISTS ix_order_obligations_product_type ON order_obligations (product_type);
CREATE INDEX IF NOT EXISTS ix_order_obligations_margin_mode ON order_obligations (margin_mode);
CREATE INDEX IF NOT EXISTS ix_order_obligations_scope_status ON order_obligations (product_type, margin_mode, status);
CREATE INDEX IF NOT EXISTS ix_order_obligations_currency_status ON order_obligations (reserve_currency, status);

CREATE TABLE IF NOT EXISTS order_states (
    client_order_id VARCHAR(64) PRIMARY KEY,
    decision_id VARCHAR(64) NOT NULL,
    intent_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
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
    reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    close_only BOOLEAN NOT NULL DEFAULT FALSE,
    td_mode VARCHAR(16),
    position_mode VARCHAR(32),
    pos_side VARCHAR(16),
    reduce_only_reason VARCHAR(64),
    close_only_reason VARCHAR(64),
    instrument_family VARCHAR(64),
    settle_currency VARCHAR(16),
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    position_intent VARCHAR(32),
    payload JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_order_states_intent_id ON order_states (intent_id);
CREATE INDEX IF NOT EXISTS ix_order_states_status ON order_states (status);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id VARCHAR(64) PRIMARY KEY,
    topic VARCHAR(128) NOT NULL,
    event_key VARCHAR(128) NOT NULL,
    source_component VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempt_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    last_error VARCHAR(512),
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_events_topic ON outbox_events (topic);
CREATE INDEX IF NOT EXISTS ix_outbox_events_event_key ON outbox_events (event_key);
CREATE INDEX IF NOT EXISTS ix_outbox_events_status ON outbox_events (status);
CREATE INDEX IF NOT EXISTS ix_outbox_events_created_at ON outbox_events (created_at);
CREATE INDEX IF NOT EXISTS ix_outbox_status_created ON outbox_events (status, created_at);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    sequence_id SERIAL PRIMARY KEY,
    snapshot_ts TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    total_equity NUMERIC(36, 18) NOT NULL,
    realized_pnl NUMERIC(36, 18) NOT NULL,
    unrealized_pnl NUMERIC(36, 18) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    primary_symbol VARCHAR(64),
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_snapshot_ts ON portfolio_snapshots (snapshot_ts);

CREATE TABLE IF NOT EXISTS position_lots (
    lot_id VARCHAR(64) PRIMARY KEY,
    symbol VARCHAR(64) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    signed_quantity_open NUMERIC(36, 18) NOT NULL,
    entry_price NUMERIC(36, 18) NOT NULL,
    source_fill_id VARCHAR(64) NOT NULL,
    target_leverage DOUBLE PRECISION NOT NULL,
    exposure_side VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_position_lots_product_type ON position_lots (product_type);
CREATE INDEX IF NOT EXISTS ix_position_lots_margin_mode ON position_lots (margin_mode);
CREATE INDEX IF NOT EXISTS ix_position_lots_symbol ON position_lots (symbol);
CREATE INDEX IF NOT EXISTS ix_position_lots_status ON position_lots (status);
CREATE INDEX IF NOT EXISTS ix_position_lots_source_fill ON position_lots (source_fill_id);
CREATE INDEX IF NOT EXISTS ix_position_lots_scope_symbol_status ON position_lots (product_type, margin_mode, symbol, status);

CREATE TABLE IF NOT EXISTS reconciliation_reports (
    reconciliation_id VARCHAR(64) PRIMARY KEY,
    decision_id VARCHAR(64),
    as_of_ts TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    severity VARCHAR(32) NOT NULL,
    halt_required BOOLEAN NOT NULL,
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    primary_symbol VARCHAR(64),
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_as_of_ts ON reconciliation_reports (as_of_ts);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_severity ON reconciliation_reports (severity);

CREATE TABLE IF NOT EXISTS runtime_profile_activation (
    activation_id VARCHAR(64) PRIMARY KEY,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_profile_revisions (
    revision_id VARCHAR(64) PRIMARY KEY,
    profile_label VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    change_classification VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by VARCHAR(128),
    supersedes_revision_id VARCHAR(64),
    activation_note VARCHAR(512),
    payload JSONB NOT NULL,
    summary JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runtime_profile_revisions_profile_label ON runtime_profile_revisions (profile_label);
CREATE INDEX IF NOT EXISTS ix_runtime_profile_revisions_status ON runtime_profile_revisions (status);
CREATE INDEX IF NOT EXISTS ix_runtime_profile_revisions_created_at ON runtime_profile_revisions (created_at);

CREATE TABLE IF NOT EXISTS strategy_profile_activation (
    activation_id VARCHAR(64) PRIMARY KEY,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    allowed_symbols_hash VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_strategy_profile_activation_scope
    ON strategy_profile_activation (product_type, margin_mode, allowed_symbols_hash);

CREATE TABLE IF NOT EXISTS strategy_profile_activation_history (
    activation_event_id VARCHAR(64) PRIMARY KEY,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_activation_history_product_type ON strategy_profile_activation_history (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_activation_history_margin_mode ON strategy_profile_activation_history (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_activation_history_executed_at ON strategy_profile_activation_history (executed_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_activation_history_scope_time
    ON strategy_profile_activation_history (product_type, margin_mode, executed_at);

CREATE TABLE IF NOT EXISTS strategy_profile_evaluations (
    evaluation_id VARCHAR(64) PRIMARY KEY,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_evaluations_product_type ON strategy_profile_evaluations (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_evaluations_margin_mode ON strategy_profile_evaluations (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_evaluations_created_at ON strategy_profile_evaluations (created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_evaluations_scope_time
    ON strategy_profile_evaluations (product_type, margin_mode, created_at);

CREATE TABLE IF NOT EXISTS strategy_profile_recommendations (
    recommendation_id VARCHAR(64) PRIMARY KEY,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    allowed_symbols_json JSONB NOT NULL,
    decision_status VARCHAR(16) NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_product_type ON strategy_profile_recommendations (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_margin_mode ON strategy_profile_recommendations (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_decision_status ON strategy_profile_recommendations (decision_status);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_generated_at ON strategy_profile_recommendations (generated_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_scope_time
    ON strategy_profile_recommendations (product_type, margin_mode, generated_at);

CREATE TABLE IF NOT EXISTS strategy_profile_rejections (
    rejection_id VARCHAR(64) PRIMARY KEY,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_rejections_product_type ON strategy_profile_rejections (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_rejections_margin_mode ON strategy_profile_rejections (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_rejections_created_at ON strategy_profile_rejections (created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_rejections_scope_time
    ON strategy_profile_rejections (product_type, margin_mode, created_at);

CREATE TABLE IF NOT EXISTS strategy_profile_revisions (
    revision_id VARCHAR(64) PRIMARY KEY,
    profile_family VARCHAR(32) NOT NULL,
    profile_id VARCHAR(64) NOT NULL,
    profile_label VARCHAR(128) NOT NULL,
    version INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    risk_level VARCHAR(16) NOT NULL,
    market_intent VARCHAR(32) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    allowed_symbols_json JSONB NOT NULL,
    hot_safe_only BOOLEAN NOT NULL,
    auto_switch_allowed BOOLEAN NOT NULL,
    manual_approval_required BOOLEAN NOT NULL,
    payload_json JSONB NOT NULL,
    guardrails_json JSONB NOT NULL,
    description VARCHAR(512),
    expected_behavior_json JSONB NOT NULL,
    created_by VARCHAR(128) NOT NULL,
    created_reason VARCHAR(64) NOT NULL,
    source_recommendation_id VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_profile_id ON strategy_profile_revisions (profile_id);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_status ON strategy_profile_revisions (status);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_product_type ON strategy_profile_revisions (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_margin_mode ON strategy_profile_revisions (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_created_at ON strategy_profile_revisions (created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_source_recommendation_id ON strategy_profile_revisions (source_recommendation_id);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_profile_status ON strategy_profile_revisions (profile_id, status);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_scope ON strategy_profile_revisions (product_type, margin_mode);

CREATE TABLE IF NOT EXISTS execution_commands (
    command_id VARCHAR(64) PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL REFERENCES execution_orders(order_id),
    command_type VARCHAR(16) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    state VARCHAR(16) NOT NULL,
    attempt_count INTEGER NOT NULL,
    last_error TEXT,
    command_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_commands_idempotency_key ON execution_commands (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_execution_commands_order_id ON execution_commands (order_id);
CREATE INDEX IF NOT EXISTS ix_execution_commands_state ON execution_commands (state);
CREATE INDEX IF NOT EXISTS ix_execution_commands_order_state ON execution_commands (order_id, state);

CREATE TABLE IF NOT EXISTS execution_fills (
    fill_id VARCHAR(64) PRIMARY KEY,
    venue_fill_id VARCHAR(128),
    order_id VARCHAR(64) NOT NULL REFERENCES execution_orders(order_id),
    venue_order_id VARCHAR(64),
    client_order_id VARCHAR(64),
    decision_id VARCHAR(64),
    intent_id VARCHAR(64),
    symbol VARCHAR(64) NOT NULL,
    side VARCHAR(8) NOT NULL,
    fill_qty NUMERIC(36, 18) NOT NULL,
    fill_price NUMERIC(36, 18) NOT NULL,
    fee_amount NUMERIC(36, 18) NOT NULL,
    fee_currency VARCHAR(16),
    reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    close_only BOOLEAN NOT NULL DEFAULT FALSE,
    td_mode VARCHAR(16),
    position_mode VARCHAR(32),
    pos_side VARCHAR(16),
    reduce_only_reason VARCHAR(64),
    close_only_reason VARCHAR(64),
    instrument_family VARCHAR(64),
    settle_currency VARCHAR(16),
    liquidity_role VARCHAR(16),
    exchange_ts TIMESTAMPTZ NOT NULL,
    ingestion_ts TIMESTAMPTZ NOT NULL,
    source_system VARCHAR(32) NOT NULL,
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_fills_source_venue_fill ON execution_fills (source_system, venue_fill_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_order_id ON execution_fills (order_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_client_order_id ON execution_fills (client_order_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_decision_id ON execution_fills (decision_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_intent_id ON execution_fills (intent_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_venue_order_id ON execution_fills (venue_order_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_source_system ON execution_fills (source_system);
CREATE INDEX IF NOT EXISTS ix_execution_fills_symbol ON execution_fills (symbol);
CREATE INDEX IF NOT EXISTS ix_execution_fills_order_ts ON execution_fills (order_id, ingestion_ts);
CREATE INDEX IF NOT EXISTS ix_execution_fills_symbol_ts ON execution_fills (symbol, ingestion_ts);

CREATE TABLE IF NOT EXISTS execution_order_state_history (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL REFERENCES execution_orders(order_id),
    from_state VARCHAR(32),
    to_state VARCHAR(32) NOT NULL,
    reason_code VARCHAR(64),
    source VARCHAR(32) NOT NULL,
    source_message_id VARCHAR(128),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_execution_order_state_history_order_id ON execution_order_state_history (order_id);
CREATE INDEX IF NOT EXISTS ix_execution_order_state_history_order ON execution_order_state_history (order_id, id);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id VARCHAR(64) PRIMARY KEY,
    journal_id VARCHAR(64) NOT NULL REFERENCES ledger_journals(journal_id),
    account_id VARCHAR(64) NOT NULL REFERENCES ledger_accounts(account_id),
    direction VARCHAR(8) NOT NULL,
    amount NUMERIC(36, 18) NOT NULL,
    currency VARCHAR(16) NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_journal_id ON ledger_entries (journal_id);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_account_id ON ledger_entries (account_id);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_currency ON ledger_entries (currency);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_source_type ON ledger_entries (source_type);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_source_id ON ledger_entries (source_id);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_journal ON ledger_entries (journal_id);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_account_effective ON ledger_entries (account_id, effective_at, created_at);

CREATE TABLE IF NOT EXISTS lot_events (
    event_id VARCHAR(64) PRIMARY KEY,
    fill_id VARCHAR(64) NOT NULL,
    lot_id VARCHAR(64) NOT NULL REFERENCES position_lots(lot_id) ON DELETE CASCADE,
    symbol VARCHAR(64) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    event_type VARCHAR(16) NOT NULL,
    quantity NUMERIC(36, 18) NOT NULL,
    entry_price NUMERIC(36, 18) NOT NULL,
    exit_price NUMERIC(36, 18),
    realized_pnl_delta NUMERIC(36, 18) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lot_events_symbol ON lot_events (symbol);
CREATE INDEX IF NOT EXISTS ix_lot_events_event_type ON lot_events (event_type);
CREATE INDEX IF NOT EXISTS ix_lot_events_fill_id ON lot_events (fill_id);
CREATE INDEX IF NOT EXISTS ix_lot_events_lot_id ON lot_events (lot_id);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id VARCHAR(64) PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL REFERENCES execution_orders(order_id),
    reserve_account_id VARCHAR(64) NOT NULL REFERENCES ledger_accounts(account_id),
    reserved_amount NUMERIC(36, 18) NOT NULL,
    consumed_amount NUMERIC(36, 18) NOT NULL,
    released_amount NUMERIC(36, 18) NOT NULL,
    state VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_reservations_order_id ON reservations (order_id);
CREATE INDEX IF NOT EXISTS ix_reservations_reserve_account_id ON reservations (reserve_account_id);
CREATE INDEX IF NOT EXISTS ix_reservations_state ON reservations (state);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id VARCHAR(64) PRIMARY KEY,
    fill_id VARCHAR(64) NOT NULL REFERENCES execution_fills(fill_id),
    order_id VARCHAR(64) NOT NULL REFERENCES execution_orders(order_id),
    journal_id VARCHAR(64) REFERENCES ledger_journals(journal_id),
    state VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    posted_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_settlements_fill_id ON settlements (fill_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_settlements_journal_id ON settlements (journal_id);
CREATE INDEX IF NOT EXISTS ix_settlements_order_id ON settlements (order_id);
CREATE INDEX IF NOT EXISTS ix_settlements_state ON settlements (state);
