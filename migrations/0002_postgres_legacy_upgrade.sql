-- AATS PostgreSQL legacy upgrade normalization
-- Auto-generated schema additions plus legacy backfills needed to upgrade older production schemas.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(256) PRIMARY KEY,
    checksum VARCHAR(128) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL
);

-- Ensure columns exist on command_outbox
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS event_id VARCHAR(64);
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS aggregate_type VARCHAR(32);
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS aggregate_id VARCHAR(64);
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS topic VARCHAR(128);
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS payload JSON;
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS status VARCHAR(16);
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS attempt_count INTEGER;
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS command_outbox ADD COLUMN IF NOT EXISTS published_at TIMESTAMP WITH TIME ZONE;

-- Ensure columns exist on decision_audit_records
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS audit_revision_id INTEGER;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS selected_strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS decision_context_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS strategy_coordinator_snapshot_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS strategy_sleeve_intent_refs JSON;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS portfolio_allocation_decision_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS baseline_assessment_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS ai_decision_brief_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS ai_market_assessment_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS ai_action_proposal_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS ai_shadow_decision_refs JSON;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS ai_shadow_evaluation_refs JSON;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS position_target_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS decision_outcome_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS policy_decision_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS risk_decision_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS execution_plan_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS execution_plan_refs JSON;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS strategy_execution_bundle_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS order_intent_refs JSON;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS order_state_refs JSON;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS fill_event_refs JSON;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS portfolio_delta_ref VARCHAR(64);
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS reconciliation_refs JSON;
ALTER TABLE IF EXISTS decision_audit_records ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on event_store
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS sequence_id INTEGER;
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS event_id VARCHAR(64);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS schema_version VARCHAR(16);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS event_type VARCHAR(128);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS event_timestamp TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS source_component VARCHAR(128);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS topic VARCHAR(128);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS event_key VARCHAR(128);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS timeframe VARCHAR(16);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS event_store ADD COLUMN IF NOT EXISTS payload JSON;

CREATE TABLE IF NOT EXISTS event_store_archive (
    archive_sequence_id SERIAL PRIMARY KEY,
    source_sequence_id INTEGER NOT NULL,
    event_id VARCHAR(64) NOT NULL,
    schema_version VARCHAR(16) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    event_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    source_component VARCHAR(128) NOT NULL,
    topic VARCHAR(128) NOT NULL,
    event_key VARCHAR(128) NOT NULL,
    decision_id VARCHAR(64),
    symbol VARCHAR(64),
    timeframe VARCHAR(16),
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    payload JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS projection_replay_offsets (
    offset_id VARCHAR(64) PRIMARY KEY,
    projection_key VARCHAR(128) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    allowed_symbols_hash VARCHAR(64) NOT NULL,
    allowed_symbols_json JSON NOT NULL,
    last_event_id VARCHAR(64),
    last_event_timestamp TIMESTAMP WITH TIME ZONE,
    baseline_generation_id VARCHAR(64),
    exchange_ack_watermark_id VARCHAR(64),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    payload JSON NOT NULL
);

-- Ensure columns exist on execution_orders
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS order_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS intent_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS client_order_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS venue_order_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS side VARCHAR(8);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS order_type VARCHAR(16);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS time_in_force VARCHAR(16);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS requested_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS limit_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS reduce_only BOOLEAN;
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS close_only BOOLEAN;
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS td_mode VARCHAR(16);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS position_mode VARCHAR(32);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS pos_side VARCHAR(16);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS reduce_only_reason VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS close_only_reason VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS instrument_family VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(16);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS strategy_family VARCHAR(32);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS strategy_bundle_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS strategy_leg_role VARCHAR(32);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS execution_action VARCHAR(32);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS position_intent VARCHAR(32);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS state VARCHAR(32);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS state_version INTEGER;
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS source_system VARCHAR(32);
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS last_exchange_ts TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS execution_orders ADD COLUMN IF NOT EXISTS raw_payload JSON;

-- Ensure columns exist on external_event_inbox
ALTER TABLE IF EXISTS external_event_inbox ADD COLUMN IF NOT EXISTS inbox_id VARCHAR(64);
ALTER TABLE IF EXISTS external_event_inbox ADD COLUMN IF NOT EXISTS source_system VARCHAR(32);
ALTER TABLE IF EXISTS external_event_inbox ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(256);
ALTER TABLE IF EXISTS external_event_inbox ADD COLUMN IF NOT EXISTS payload JSON;
ALTER TABLE IF EXISTS external_event_inbox ADD COLUMN IF NOT EXISTS received_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS external_event_inbox ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS external_event_inbox ADD COLUMN IF NOT EXISTS processing_result VARCHAR(16);
ALTER TABLE IF EXISTS external_event_inbox ADD COLUMN IF NOT EXISTS last_error TEXT;

-- Ensure columns exist on fill_events
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS fill_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS intent_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS client_order_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS exchange_order_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS symbol VARCHAR(32);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS side VARCHAR(8);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS fill_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS fill_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS fee_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS reduce_only BOOLEAN;
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS close_only BOOLEAN;
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS td_mode VARCHAR(16);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS position_mode VARCHAR(32);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS pos_side VARCHAR(16);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS reduce_only_reason VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS close_only_reason VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS instrument_family VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(16);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS strategy_family VARCHAR(32);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS strategy_bundle_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS strategy_leg_role VARCHAR(32);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS position_intent VARCHAR(32);
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS exchange_timestamp TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS ingestion_timestamp TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS fill_events ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on fill_outcomes
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS fill_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS intent_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS order_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS symbol VARCHAR(32);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS venue VARCHAR(16);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS side VARCHAR(8);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS fill_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS fill_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS fill_notional NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS fee_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS fee_currency VARCHAR(16);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS liquidity_role VARCHAR(16);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS exchange_timestamp TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS ingestion_timestamp TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS order_status_after_fill VARCHAR(32);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS strategy_family VARCHAR(32);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS strategy_bundle_id VARCHAR(64);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS strategy_leg_role VARCHAR(32);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS target_leverage FLOAT;
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS exposure_side VARCHAR(16);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS execution_action VARCHAR(16);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS position_intent VARCHAR(32);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS starting_position_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS starting_avg_entry_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS ending_position_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS ending_avg_entry_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS realized_pnl_delta NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS fee_delta NUMERIC(36, 18);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS fill_outcomes ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on funding_fee_records
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS bill_id VARCHAR(64);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS currency VARCHAR(16);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS balance_after NUMERIC(36, 18);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS bill_type VARCHAR(16);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS sub_type VARCHAR(16);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS type_label VARCHAR(64);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS sub_type_label VARCHAR(64);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS semantic_group VARCHAR(32);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS funding_direction VARCHAR(16);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS bill_ts TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS ledger_posting_state VARCHAR(16);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS ledger_journal_id VARCHAR(64);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS ledger_posted_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS funding_fee_records ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on ledger_accounts
ALTER TABLE IF EXISTS ledger_accounts ADD COLUMN IF NOT EXISTS account_id VARCHAR(64);
ALTER TABLE IF EXISTS ledger_accounts ADD COLUMN IF NOT EXISTS account_type VARCHAR(32);
ALTER TABLE IF EXISTS ledger_accounts ADD COLUMN IF NOT EXISTS currency VARCHAR(16);
ALTER TABLE IF EXISTS ledger_accounts ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS ledger_accounts ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS ledger_accounts ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS ledger_accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;

-- Ensure columns exist on ledger_journals
ALTER TABLE IF EXISTS ledger_journals ADD COLUMN IF NOT EXISTS journal_id VARCHAR(64);
ALTER TABLE IF EXISTS ledger_journals ADD COLUMN IF NOT EXISTS journal_type VARCHAR(32);
ALTER TABLE IF EXISTS ledger_journals ADD COLUMN IF NOT EXISTS source_type VARCHAR(32);
ALTER TABLE IF EXISTS ledger_journals ADD COLUMN IF NOT EXISTS source_id VARCHAR(64);
ALTER TABLE IF EXISTS ledger_journals ADD COLUMN IF NOT EXISTS status VARCHAR(16);
ALTER TABLE IF EXISTS ledger_journals ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS ledger_journals ADD COLUMN IF NOT EXISTS posted_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS ledger_journals ADD COLUMN IF NOT EXISTS metadata JSON;

-- Ensure columns exist on operator_users
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS user_id VARCHAR(64);
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS username VARCHAR(128);
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(512);
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS role VARCHAR(16);
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS enabled BOOLEAN;
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS last_failed_login_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER;
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS operator_users ADD COLUMN IF NOT EXISTS payload JSON;
UPDATE operator_users SET failed_login_attempts = 0 WHERE failed_login_attempts IS NULL;

-- Ensure columns exist on order_obligations
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS client_order_id VARCHAR(64);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS obligation_id VARCHAR(64);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS intent_id VARCHAR(64);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS symbol VARCHAR(32);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS reserve_currency VARCHAR(16);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS status VARCHAR(32);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS reserved_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS consumed_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS released_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS strategy_family VARCHAR(32);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS strategy_bundle_id VARCHAR(64);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS strategy_leg_role VARCHAR(32);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS last_update_ts TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS order_obligations ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on order_states
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS client_order_id VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS intent_id VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS symbol VARCHAR(32);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS exchange_order_id VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS status VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS submitted_ts TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS last_update_ts TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS requested_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS filled_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS remaining_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS average_fill_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS fees NUMERIC(36, 18);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS reduce_only BOOLEAN;
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS close_only BOOLEAN;
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS td_mode VARCHAR(16);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS position_mode VARCHAR(32);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS pos_side VARCHAR(16);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS reduce_only_reason VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS close_only_reason VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS instrument_family VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(16);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS strategy_family VARCHAR(32);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS strategy_bundle_id VARCHAR(64);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS strategy_leg_role VARCHAR(32);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS position_intent VARCHAR(32);
ALTER TABLE IF EXISTS order_states ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on outbox_events
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS event_id VARCHAR(64);
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS topic VARCHAR(128);
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS event_key VARCHAR(128);
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS source_component VARCHAR(128);
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS status VARCHAR(16);
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS attempt_count INTEGER;
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS published_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS last_error VARCHAR(512);
ALTER TABLE IF EXISTS outbox_events ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on portfolio_allocation_decisions
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS allocator_version VARCHAR(64);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS automatic_enabled BOOLEAN;
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS route_action VARCHAR(32);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS primary_family VARCHAR(32);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS primary_strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS portfolio_requested_notional NUMERIC(36, 18);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS portfolio_approved_notional NUMERIC(36, 18);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS portfolio_budget_cut_notional NUMERIC(36, 18);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS expected_edge_bps NUMERIC(36, 18);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS expected_cost_bps NUMERIC(36, 18);
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS portfolio_allocation_decisions ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on portfolio_snapshots
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS sequence_id INTEGER;
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS snapshot_ts TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS total_equity NUMERIC(36, 18);
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS realized_pnl NUMERIC(36, 18);
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS unrealized_pnl NUMERIC(36, 18);
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS primary_symbol VARCHAR(64);
ALTER TABLE IF EXISTS portfolio_snapshots ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on position_lots
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS lot_id VARCHAR(64);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS signed_quantity_open NUMERIC(36, 18);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS entry_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS source_fill_id VARCHAR(64);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS target_leverage FLOAT;
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS exposure_side VARCHAR(16);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS status VARCHAR(16);
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS opened_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS position_lots ADD COLUMN IF NOT EXISTS metadata JSON;

-- Ensure columns exist on reconciliation_reports
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS reconciliation_id VARCHAR(64);
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS as_of_ts TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS severity VARCHAR(32);
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS halt_required BOOLEAN;
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS primary_symbol VARCHAR(64);
ALTER TABLE IF EXISTS reconciliation_reports ADD COLUMN IF NOT EXISTS payload JSON;

CREATE TABLE IF NOT EXISTS reconciliation_findings (
    finding_id VARCHAR(64) PRIMARY KEY,
    reconciliation_id VARCHAR(64) NOT NULL REFERENCES reconciliation_reports(reconciliation_id),
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    primary_symbol VARCHAR(64),
    strategy_sleeve_id VARCHAR(64),
    allocation_id VARCHAR(64),
    strategy_bundle_id VARCHAR(64),
    scope_kind VARCHAR(32) NOT NULL,
    scope_ref VARCHAR(128),
    layer VARCHAR(32) NOT NULL,
    finding_type VARCHAR(64) NOT NULL,
    severity_class VARCHAR(16) NOT NULL,
    structural BOOLEAN NOT NULL DEFAULT FALSE,
    financial BOOLEAN NOT NULL DEFAULT FALSE,
    observational BOOLEAN NOT NULL DEFAULT FALSE,
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    only_reduce_required BOOLEAN NOT NULL DEFAULT FALSE,
    halt_required BOOLEAN NOT NULL DEFAULT FALSE,
    blocks_resume BOOLEAN NOT NULL DEFAULT FALSE,
    reason_code VARCHAR(128) NOT NULL,
    details JSON NOT NULL DEFAULT '{}'::json,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS baseline_generations (
    generation_id VARCHAR(64) PRIMARY KEY,
    baseline_event_ref VARCHAR(64) NOT NULL,
    baseline_id VARCHAR(64),
    baseline_kind VARCHAR(32) NOT NULL,
    account_source VARCHAR(64) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    allowed_symbols JSON NOT NULL DEFAULT '[]'::json,
    exchange_snapshot_ts TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    imported_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    safe_for_automatic_continuation BOOLEAN NOT NULL DEFAULT TRUE,
    requires_operator_review BOOLEAN NOT NULL DEFAULT FALSE,
    previous_generation_id VARCHAR(64),
    previous_baseline_ref VARCHAR(64),
    exchange_ack_watermark_id VARCHAR(64),
    operator_action_ref VARCHAR(64),
    trigger_reason VARCHAR(256),
    reason_codes JSON NOT NULL DEFAULT '[]'::json,
    balance_count INTEGER NOT NULL DEFAULT 0,
    position_count INTEGER NOT NULL DEFAULT 0,
    open_order_count INTEGER NOT NULL DEFAULT 0,
    fill_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS exchange_ack_watermarks (
    watermark_id VARCHAR(64) PRIMARY KEY,
    account_source VARCHAR(64) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    allowed_symbols JSON NOT NULL DEFAULT '[]'::json,
    acknowledged_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    latest_bill_id VARCHAR(128),
    latest_bill_ts TIMESTAMP WITH TIME ZONE,
    latest_fill_id VARCHAR(128),
    latest_fill_ts TIMESTAMP WITH TIME ZONE,
    latest_order_snapshot_ts TIMESTAMP WITH TIME ZONE,
    latest_reconciliation_id VARCHAR(64),
    baseline_event_ref VARCHAR(64),
    operator_action_ref VARCHAR(64),
    details JSON NOT NULL DEFAULT '{}'::json,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reconciliation_state_snapshots (
    snapshot_id VARCHAR(64) PRIMARY KEY,
    reconciliation_id VARCHAR(64) NOT NULL REFERENCES reconciliation_reports(reconciliation_id),
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    primary_symbol VARCHAR(64),
    recovery_state VARCHAR(32) NOT NULL,
    resume_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    safe_to_trade BOOLEAN NOT NULL DEFAULT FALSE,
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    only_reduce_required BOOLEAN NOT NULL DEFAULT FALSE,
    halt_required BOOLEAN NOT NULL DEFAULT FALSE,
    bundle_recovery_required BOOLEAN NOT NULL DEFAULT FALSE,
    resume_blocked_reasons_json JSON NOT NULL DEFAULT '[]'::json,
    derived_from_generation_id VARCHAR(64),
    exchange_ack_watermark_id VARCHAR(64),
    details JSON NOT NULL DEFAULT '{}'::json,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Drop deprecated runtime profile control tables
DROP TABLE IF EXISTS runtime_profile_activation CASCADE;
DROP TABLE IF EXISTS runtime_profile_revisions CASCADE;

-- Ensure columns exist on sleeve_pnl_records
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS record_id VARCHAR(96);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS strategy_family VARCHAR(32);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS strategy_bundle_id VARCHAR(64);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS strategy_leg_role VARCHAR(32);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS event_type VARCHAR(32);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS fill_id VARCHAR(64);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS funding_fee_id VARCHAR(64);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS fee_currency VARCHAR(16);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS realized_pnl NUMERIC(36, 18);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS fee_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS funding_fee_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS inventory_move_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS attribution_type VARCHAR(32);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS event_timestamp TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS sleeve_pnl_records ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on strategy_execution_bundles
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS bundle_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS family VARCHAR(32);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS route_action VARCHAR(32);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS bundle_type VARCHAR(32);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS bundle_priority VARCHAR(32);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS status VARCHAR(32);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS selected_symbol VARCHAR(64);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS gross_requested_exposure NUMERIC(36, 18);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS net_approved_exposure NUMERIC(36, 18);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS expected_cost_bps NUMERIC(36, 18);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS expected_edge_bps NUMERIC(36, 18);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS portfolio_risk_budget_state VARCHAR(32);
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_execution_bundles ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on strategy_profile_activation
ALTER TABLE IF EXISTS strategy_profile_activation ADD COLUMN IF NOT EXISTS activation_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_activation ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_activation ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_activation ADD COLUMN IF NOT EXISTS allowed_symbols_hash VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_activation ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on strategy_profile_activation_history
ALTER TABLE IF EXISTS strategy_profile_activation_history ADD COLUMN IF NOT EXISTS activation_event_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_activation_history ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_activation_history ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_activation_history ADD COLUMN IF NOT EXISTS executed_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_profile_activation_history ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on strategy_profile_evaluations
ALTER TABLE IF EXISTS strategy_profile_evaluations ADD COLUMN IF NOT EXISTS evaluation_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_evaluations ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_evaluations ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_evaluations ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_profile_evaluations ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on strategy_profile_recommendations
ALTER TABLE IF EXISTS strategy_profile_recommendations ADD COLUMN IF NOT EXISTS recommendation_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_recommendations ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_recommendations ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_recommendations ADD COLUMN IF NOT EXISTS allowed_symbols_json JSON;
ALTER TABLE IF EXISTS strategy_profile_recommendations ADD COLUMN IF NOT EXISTS decision_status VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_recommendations ADD COLUMN IF NOT EXISTS generated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_profile_recommendations ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_profile_recommendations ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on strategy_profile_rejections
ALTER TABLE IF EXISTS strategy_profile_rejections ADD COLUMN IF NOT EXISTS rejection_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_rejections ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_rejections ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_rejections ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_profile_rejections ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on strategy_profile_revisions
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS revision_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS profile_family VARCHAR(32);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS profile_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS profile_label VARCHAR(128);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS version INTEGER;
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS status VARCHAR(32);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS risk_level VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS market_intent VARCHAR(32);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS allowed_symbols_json JSON;
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS hot_safe_only BOOLEAN;
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS auto_switch_allowed BOOLEAN;
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS manual_approval_required BOOLEAN;
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS payload_json JSON;
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS guardrails_json JSON;
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS description VARCHAR(512);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS expected_behavior_json JSON;
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS created_by VARCHAR(128);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS created_reason VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS source_recommendation_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_profile_revisions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;

-- Ensure columns exist on strategy_sleeve_intents
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS sleeve_intent_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS family VARCHAR(32);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS state VARCHAR(32);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS inventory_policy VARCHAR(32);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS route_action VARCHAR(32);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS automatic_enabled BOOLEAN;
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS budget_multiplier NUMERIC(36, 18);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS allocator_weight NUMERIC(36, 18);
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_sleeve_intents ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on strategy_sleeves
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS family VARCHAR(32);
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS name VARCHAR(128);
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS product_scope VARCHAR(16);
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS margin_scope VARCHAR(16);
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS automatic_enabled BOOLEAN;
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS inventory_policy VARCHAR(32);
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS status VARCHAR(16);
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS strategy_sleeves ADD COLUMN IF NOT EXISTS payload JSON;

CREATE TABLE IF NOT EXISTS sleeve_budget_profiles (
    budget_profile_id VARCHAR(64) PRIMARY KEY,
    family VARCHAR(32) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    symbol_scope_json JSON NOT NULL,
    quote_budget_limit NUMERIC(36, 18),
    margin_budget_limit NUMERIC(36, 18),
    notional_cap NUMERIC(36, 18),
    max_symbol_notional NUMERIC(36, 18),
    max_drawdown_usdt NUMERIC(36, 18),
    allocator_base_weight NUMERIC(36, 18) NOT NULL,
    hedge_priority_class VARCHAR(32) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    payload JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS sleeve_budget_assignments (
    assignment_id VARCHAR(64) PRIMARY KEY,
    budget_profile_id VARCHAR(64) NOT NULL,
    strategy_sleeve_id VARCHAR(64) NOT NULL,
    family VARCHAR(32) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    active_budget_multiplier NUMERIC(36, 18) NOT NULL,
    allocator_base_weight NUMERIC(36, 18) NOT NULL,
    effective_quote_budget_limit NUMERIC(36, 18),
    effective_margin_budget_limit NUMERIC(36, 18),
    effective_notional_cap NUMERIC(36, 18),
    effective_max_symbol_notional NUMERIC(36, 18),
    hedge_priority_class VARCHAR(32) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    payload JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS allocator_budget_snapshots (
    budget_snapshot_id VARCHAR(64) PRIMARY KEY,
    allocation_id VARCHAR(64) NOT NULL,
    strategy_sleeve_id VARCHAR(64) NOT NULL,
    family VARCHAR(32) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    requested_notional NUMERIC(36, 18) NOT NULL,
    approved_notional NUMERIC(36, 18) NOT NULL,
    requested_delta_qty NUMERIC(36, 18) NOT NULL,
    approved_delta_qty NUMERIC(36, 18) NOT NULL,
    budget_multiplier NUMERIC(36, 18) NOT NULL,
    allocator_weight NUMERIC(36, 18) NOT NULL,
    quote_budget_limit NUMERIC(36, 18),
    margin_budget_limit NUMERIC(36, 18),
    notional_cap NUMERIC(36, 18),
    max_symbol_notional NUMERIC(36, 18),
    hedge_priority_class VARCHAR(32) NOT NULL,
    priority_rank INTEGER NOT NULL,
    portfolio_requested_notional NUMERIC(36, 18) NOT NULL,
    portfolio_approved_notional NUMERIC(36, 18) NOT NULL,
    portfolio_budget_cut_notional NUMERIC(36, 18) NOT NULL,
    clamped BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    payload JSON NOT NULL
);
ALTER TABLE IF EXISTS allocator_budget_snapshots ADD COLUMN IF NOT EXISTS priority_rank INTEGER;
ALTER TABLE IF EXISTS allocator_budget_snapshots ADD COLUMN IF NOT EXISTS portfolio_requested_notional NUMERIC(36, 18);
ALTER TABLE IF EXISTS allocator_budget_snapshots ADD COLUMN IF NOT EXISTS portfolio_approved_notional NUMERIC(36, 18);
ALTER TABLE IF EXISTS allocator_budget_snapshots ADD COLUMN IF NOT EXISTS portfolio_budget_cut_notional NUMERIC(36, 18);

CREATE TABLE IF NOT EXISTS allocator_conflict_resolutions (
    conflict_resolution_id VARCHAR(64) PRIMARY KEY,
    allocation_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    conflict_type VARCHAR(64) NOT NULL,
    resolution_action VARCHAR(64) NOT NULL,
    gross_requested_qty NUMERIC(36, 18) NOT NULL,
    net_approved_qty NUMERIC(36, 18) NOT NULL,
    blocked_qty NUMERIC(36, 18) NOT NULL,
    protected_notional NUMERIC(36, 18) NOT NULL,
    reduced_notional NUMERIC(36, 18) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    payload JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS allocator_netting_decisions (
    netting_decision_id VARCHAR(64) PRIMARY KEY,
    allocation_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    gross_buy_qty NUMERIC(36, 18) NOT NULL,
    gross_sell_qty NUMERIC(36, 18) NOT NULL,
    net_approved_qty NUMERIC(36, 18) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    payload JSON NOT NULL
);

-- Ensure columns exist on execution_commands
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS command_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS order_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS command_type VARCHAR(16);
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS state VARCHAR(16);
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS attempt_count INTEGER;
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS command_payload JSON;
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS execution_commands ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;

-- Ensure columns exist on execution_fills
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS fill_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS venue_fill_id VARCHAR(128);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS order_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS venue_order_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS client_order_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS decision_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS intent_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS side VARCHAR(8);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS fill_qty NUMERIC(36, 18);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS fill_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS fee_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS fee_currency VARCHAR(16);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS reduce_only BOOLEAN;
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS close_only BOOLEAN;
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS td_mode VARCHAR(16);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS position_mode VARCHAR(32);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS pos_side VARCHAR(16);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS reduce_only_reason VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS close_only_reason VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS instrument_family VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(16);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS strategy_family VARCHAR(32);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS strategy_bundle_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS strategy_leg_role VARCHAR(32);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS liquidity_role VARCHAR(16);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS exchange_ts TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS ingestion_ts TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS source_system VARCHAR(32);
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS raw_payload JSON;
ALTER TABLE IF EXISTS execution_fills ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;

-- Ensure columns exist on execution_order_state_history
ALTER TABLE IF EXISTS execution_order_state_history ADD COLUMN IF NOT EXISTS id INTEGER;
ALTER TABLE IF EXISTS execution_order_state_history ADD COLUMN IF NOT EXISTS order_id VARCHAR(64);
ALTER TABLE IF EXISTS execution_order_state_history ADD COLUMN IF NOT EXISTS from_state VARCHAR(32);
ALTER TABLE IF EXISTS execution_order_state_history ADD COLUMN IF NOT EXISTS to_state VARCHAR(32);
ALTER TABLE IF EXISTS execution_order_state_history ADD COLUMN IF NOT EXISTS reason_code VARCHAR(64);
ALTER TABLE IF EXISTS execution_order_state_history ADD COLUMN IF NOT EXISTS source VARCHAR(32);
ALTER TABLE IF EXISTS execution_order_state_history ADD COLUMN IF NOT EXISTS source_message_id VARCHAR(128);
ALTER TABLE IF EXISTS execution_order_state_history ADD COLUMN IF NOT EXISTS payload JSON;
ALTER TABLE IF EXISTS execution_order_state_history ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;

-- Ensure columns exist on ledger_entries
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS entry_id VARCHAR(64);
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS journal_id VARCHAR(64);
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS account_id VARCHAR(64);
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS direction VARCHAR(8);
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS currency VARCHAR(16);
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS effective_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS source_type VARCHAR(32);
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS source_id VARCHAR(64);
ALTER TABLE IF EXISTS ledger_entries ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;

-- Ensure columns exist on lot_events
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS event_id VARCHAR(64);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS fill_id VARCHAR(64);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS lot_id VARCHAR(64);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS strategy_sleeve_id VARCHAR(64);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS allocation_id VARCHAR(64);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS product_type VARCHAR(16);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS event_type VARCHAR(16);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS quantity NUMERIC(36, 18);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS entry_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS exit_price NUMERIC(36, 18);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS realized_pnl_delta NUMERIC(36, 18);
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS lot_events ADD COLUMN IF NOT EXISTS payload JSON;

-- Ensure columns exist on reservations
ALTER TABLE IF EXISTS reservations ADD COLUMN IF NOT EXISTS reservation_id VARCHAR(64);
ALTER TABLE IF EXISTS reservations ADD COLUMN IF NOT EXISTS order_id VARCHAR(64);
ALTER TABLE IF EXISTS reservations ADD COLUMN IF NOT EXISTS reserve_account_id VARCHAR(64);
ALTER TABLE IF EXISTS reservations ADD COLUMN IF NOT EXISTS reserved_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS reservations ADD COLUMN IF NOT EXISTS consumed_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS reservations ADD COLUMN IF NOT EXISTS released_amount NUMERIC(36, 18);
ALTER TABLE IF EXISTS reservations ADD COLUMN IF NOT EXISTS state VARCHAR(32);
ALTER TABLE IF EXISTS reservations ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS reservations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;

-- Ensure columns exist on settlements
ALTER TABLE IF EXISTS settlements ADD COLUMN IF NOT EXISTS settlement_id VARCHAR(64);
ALTER TABLE IF EXISTS settlements ADD COLUMN IF NOT EXISTS fill_id VARCHAR(64);
ALTER TABLE IF EXISTS settlements ADD COLUMN IF NOT EXISTS order_id VARCHAR(64);
ALTER TABLE IF EXISTS settlements ADD COLUMN IF NOT EXISTS journal_id VARCHAR(64);
ALTER TABLE IF EXISTS settlements ADD COLUMN IF NOT EXISTS state VARCHAR(32);
ALTER TABLE IF EXISTS settlements ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE IF EXISTS settlements ADD COLUMN IF NOT EXISTS posted_at TIMESTAMP WITH TIME ZONE;

-- Legacy backfills for event store scope fields
UPDATE event_store
SET
    symbol = COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0, symbol),
    timeframe = COALESCE(NULLIF(payload->>'timeframe', ''), timeframe),
    product_type = COALESCE(
        NULLIF(payload->>'product_type', ''),
        CASE
            WHEN COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0, symbol) LIKE '%-SWAP' THEN 'derivatives'
            WHEN COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0, symbol) ~ '-[0-9]{6}$' THEN 'derivatives'
            ELSE NULL
        END,
        product_type
    ),
    margin_mode = COALESCE(
        NULLIF(payload->>'margin_mode', ''),
        CASE
            WHEN COALESCE(
                NULLIF(payload->>'product_type', ''),
                CASE
                    WHEN COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0, symbol) LIKE '%-SWAP' THEN 'derivatives'
                    WHEN COALESCE(NULLIF(payload->>'symbol', ''), payload->'allowed_symbols'->>0, symbol) ~ '-[0-9]{6}$' THEN 'derivatives'
                    ELSE 'spot'
                END
            ) = 'spot' THEN 'cash' ELSE 'cross' END,
        margin_mode
    )
WHERE symbol IS NULL OR timeframe IS NULL OR product_type IS NULL OR margin_mode IS NULL;

-- Legacy backfills for portfolio snapshots
UPDATE portfolio_snapshots
SET
    product_type = COALESCE(NULLIF(payload->>'product_type', ''), product_type, 'spot'),
    margin_mode = COALESCE(NULLIF(payload->>'margin_mode', ''), margin_mode, 'cash'),
    primary_symbol = COALESCE(NULLIF(payload->'positions'->0->>'symbol', ''), primary_symbol)
WHERE product_type IS NULL OR margin_mode IS NULL OR primary_symbol IS NULL;
ALTER TABLE IF EXISTS portfolio_snapshots ALTER COLUMN product_type SET NOT NULL;
ALTER TABLE IF EXISTS portfolio_snapshots ALTER COLUMN margin_mode SET NOT NULL;

-- Legacy backfills for order states
UPDATE order_states
SET
    decision_id = COALESCE(NULLIF(payload->>'decision_id', ''), decision_id, 'legacy_unknown'),
    symbol = COALESCE(NULLIF(payload->>'symbol', ''), symbol, 'legacy_unknown'),
    product_type = COALESCE(
        NULLIF(payload->>'product_type', ''),
        CASE
            WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) LIKE '%-SWAP' THEN 'derivatives'
            WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) ~ '-[0-9]{6}$' THEN 'derivatives'
            ELSE 'spot'
        END,
        product_type
    ),
    margin_mode = COALESCE(
        NULLIF(payload->>'margin_mode', ''),
        NULLIF(payload->'submission_payload'->>'tdMode', ''),
        CASE
            WHEN COALESCE(
                NULLIF(payload->>'product_type', ''),
                CASE
                    WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) LIKE '%-SWAP' THEN 'derivatives'
                    WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) ~ '-[0-9]{6}$' THEN 'derivatives'
                    ELSE 'spot'
                END
            ) = 'spot' THEN 'cash' ELSE 'cross' END,
        margin_mode
    ),
    position_intent = COALESCE(NULLIF(payload->>'position_intent', ''), position_intent),
    reduce_only = COALESCE(reduce_only, false),
    close_only = COALESCE(close_only, false),
    td_mode = COALESCE(NULLIF(payload->>'td_mode', ''), NULLIF(payload->'submission_payload'->>'tdMode', ''), td_mode),
    position_mode = COALESCE(NULLIF(payload->>'position_mode', ''), position_mode, 'net_mode'),
    pos_side = COALESCE(NULLIF(payload->>'pos_side', ''), pos_side, 'net'),
    instrument_family = COALESCE(NULLIF(payload->>'instrument_family', ''), instrument_family, symbol),
    settle_currency = COALESCE(NULLIF(payload->>'settle_currency', ''), settle_currency, 'USDT')
WHERE decision_id IS NULL OR symbol IS NULL OR product_type IS NULL OR margin_mode IS NULL OR position_intent IS NULL OR reduce_only IS NULL OR close_only IS NULL OR td_mode IS NULL OR position_mode IS NULL OR pos_side IS NULL OR instrument_family IS NULL OR settle_currency IS NULL;
ALTER TABLE IF EXISTS order_states ALTER COLUMN decision_id SET NOT NULL;
ALTER TABLE IF EXISTS order_states ALTER COLUMN symbol SET NOT NULL;

-- Legacy backfills for fill events
UPDATE fill_events
SET
    product_type = COALESCE(
        NULLIF(payload->>'product_type', ''),
        CASE
            WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) LIKE '%-SWAP' THEN 'derivatives'
            WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) ~ '-[0-9]{6}$' THEN 'derivatives'
            ELSE 'spot'
        END,
        product_type
    ),
    margin_mode = COALESCE(
        NULLIF(payload->>'margin_mode', ''),
        CASE
            WHEN COALESCE(
                NULLIF(payload->>'product_type', ''),
                CASE
                    WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) LIKE '%-SWAP' THEN 'derivatives'
                    WHEN COALESCE(NULLIF(payload->>'symbol', ''), symbol) ~ '-[0-9]{6}$' THEN 'derivatives'
                    ELSE 'spot'
                END
            ) = 'spot' THEN 'cash' ELSE 'cross' END,
        margin_mode
    ),
    position_intent = COALESCE(NULLIF(payload->>'position_intent', ''), position_intent),
    reduce_only = COALESCE(reduce_only, false),
    close_only = COALESCE(close_only, false),
    td_mode = COALESCE(NULLIF(payload->>'td_mode', ''), td_mode),
    position_mode = COALESCE(NULLIF(payload->>'position_mode', ''), position_mode, 'net_mode'),
    pos_side = COALESCE(NULLIF(payload->>'pos_side', ''), pos_side, 'net'),
    instrument_family = COALESCE(NULLIF(payload->>'instrument_family', ''), instrument_family, symbol),
    settle_currency = COALESCE(NULLIF(payload->>'settle_currency', ''), settle_currency, 'USDT')
WHERE product_type IS NULL OR margin_mode IS NULL OR position_intent IS NULL OR reduce_only IS NULL OR close_only IS NULL OR td_mode IS NULL OR position_mode IS NULL OR pos_side IS NULL OR instrument_family IS NULL OR settle_currency IS NULL;

-- Legacy backfills for reconciliation reports
UPDATE reconciliation_reports
SET
    decision_id = COALESCE(NULLIF(payload->>'decision_id', ''), decision_id, 'legacy_unknown'),
    product_type = COALESCE(NULLIF(payload->>'product_type', ''), product_type, 'spot'),
    margin_mode = COALESCE(NULLIF(payload->>'margin_mode', ''), margin_mode, 'cash'),
    primary_symbol = COALESCE(
        NULLIF(payload->>'primary_symbol', ''),
        NULLIF(payload->'portfolio'->'positions'->0->>'symbol', ''),
        payload->'allowed_symbols'->>0,
        primary_symbol
    )
WHERE decision_id IS NULL OR product_type IS NULL OR margin_mode IS NULL OR primary_symbol IS NULL;

-- Normalize legacy reservation and settlement status widths
ALTER TABLE IF EXISTS reservations ALTER COLUMN state TYPE VARCHAR(32);
ALTER TABLE IF EXISTS settlements ALTER COLUMN state TYPE VARCHAR(32);

-- Normalize legacy lot_events foreign key to current cascade behavior
ALTER TABLE IF EXISTS lot_events DROP CONSTRAINT IF EXISTS lot_events_lot_id_fkey;
ALTER TABLE IF EXISTS lot_events
    ADD CONSTRAINT lot_events_lot_id_fkey
    FOREIGN KEY (lot_id) REFERENCES position_lots(lot_id) ON DELETE CASCADE;

-- Backfill newer audit references from payload when historical rows exist
UPDATE decision_audit_records
SET
    selected_strategy_sleeve_id = COALESCE(NULLIF(payload->>'selected_strategy_sleeve_id', ''), selected_strategy_sleeve_id),
    allocation_id = COALESCE(NULLIF(payload->>'allocation_id', ''), allocation_id),
    strategy_coordinator_snapshot_ref = COALESCE(NULLIF(payload->>'strategy_coordinator_snapshot_ref', ''), strategy_coordinator_snapshot_ref),
    portfolio_allocation_decision_ref = COALESCE(NULLIF(payload->>'portfolio_allocation_decision_ref', ''), portfolio_allocation_decision_ref),
    ai_decision_brief_ref = COALESCE(NULLIF(payload->>'ai_decision_brief_ref', ''), ai_decision_brief_ref),
    decision_outcome_ref = COALESCE(NULLIF(payload->>'decision_outcome_ref', ''), decision_outcome_ref),
    execution_plan_ref = COALESCE(NULLIF(payload->>'execution_plan_ref', ''), execution_plan_ref),
    strategy_execution_bundle_ref = COALESCE(NULLIF(payload->>'strategy_execution_bundle_ref', ''), strategy_execution_bundle_ref),
    order_state_refs = COALESCE(order_state_refs, CAST(payload->'order_state_refs' AS json), '[]'::json),
    strategy_sleeve_intent_refs = COALESCE(strategy_sleeve_intent_refs, CAST(payload->'strategy_sleeve_intent_refs' AS json), '[]'::json),
    ai_shadow_decision_refs = COALESCE(ai_shadow_decision_refs, CAST(payload->'ai_shadow_decision_refs' AS json), '[]'::json),
    ai_shadow_evaluation_refs = COALESCE(ai_shadow_evaluation_refs, CAST(payload->'ai_shadow_evaluation_refs' AS json), '[]'::json),
    execution_plan_refs = COALESCE(execution_plan_refs, CAST(payload->'execution_plan_refs' AS json), '[]'::json)
WHERE selected_strategy_sleeve_id IS NULL OR allocation_id IS NULL OR strategy_coordinator_snapshot_ref IS NULL OR portfolio_allocation_decision_ref IS NULL OR ai_decision_brief_ref IS NULL OR decision_outcome_ref IS NULL OR execution_plan_ref IS NULL OR strategy_execution_bundle_ref IS NULL OR order_state_refs IS NULL OR strategy_sleeve_intent_refs IS NULL OR ai_shadow_decision_refs IS NULL OR ai_shadow_evaluation_refs IS NULL OR execution_plan_refs IS NULL;

-- Ensure indexes exist on command_outbox
CREATE INDEX IF NOT EXISTS ix_command_outbox_aggregate_id ON command_outbox (aggregate_id);
CREATE INDEX IF NOT EXISTS ix_command_outbox_aggregate_type ON command_outbox (aggregate_type);
CREATE INDEX IF NOT EXISTS ix_command_outbox_status ON command_outbox (status);
CREATE INDEX IF NOT EXISTS ix_command_outbox_status_created ON command_outbox (status, created_at);
CREATE INDEX IF NOT EXISTS ix_command_outbox_topic ON command_outbox (topic);

-- Ensure indexes exist on decision_audit_records
CREATE INDEX IF NOT EXISTS ix_decision_audit_records_allocation_id ON decision_audit_records (allocation_id);
CREATE INDEX IF NOT EXISTS ix_decision_audit_records_decision_id ON decision_audit_records (decision_id);
CREATE INDEX IF NOT EXISTS ix_decision_audit_records_selected_strategy_sleeve_id ON decision_audit_records (selected_strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_decision_audit_records_updated_at ON decision_audit_records (updated_at);

-- Ensure indexes exist on event_store
CREATE INDEX IF NOT EXISTS ix_event_store_decision_id ON event_store (decision_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_event_store_event_id ON event_store (event_id);
CREATE INDEX IF NOT EXISTS ix_event_store_event_key ON event_store (event_key);
CREATE INDEX IF NOT EXISTS ix_event_store_event_timestamp ON event_store (event_timestamp);
CREATE INDEX IF NOT EXISTS ix_event_store_event_type ON event_store (event_type);
CREATE INDEX IF NOT EXISTS ix_event_store_margin_mode ON event_store (margin_mode);
CREATE INDEX IF NOT EXISTS ix_event_store_product_type ON event_store (product_type);
CREATE INDEX IF NOT EXISTS ix_event_store_symbol ON event_store (symbol);
CREATE INDEX IF NOT EXISTS ix_event_store_timeframe ON event_store (timeframe);
CREATE INDEX IF NOT EXISTS ix_event_store_topic ON event_store (topic);
CREATE INDEX IF NOT EXISTS ix_event_store_topic_scope_seq ON event_store (topic, product_type, margin_mode, sequence_id);
CREATE INDEX IF NOT EXISTS ix_event_store_topic_symbol_seq ON event_store (topic, symbol, sequence_id);

-- Ensure indexes exist on event_store_archive
CREATE UNIQUE INDEX IF NOT EXISTS ix_event_store_archive_source_sequence_id ON event_store_archive (source_sequence_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_event_store_archive_event_id ON event_store_archive (event_id);
CREATE INDEX IF NOT EXISTS ix_event_store_archive_event_timestamp ON event_store_archive (event_timestamp);
CREATE INDEX IF NOT EXISTS ix_event_store_archive_topic ON event_store_archive (topic);
CREATE INDEX IF NOT EXISTS ix_event_store_archive_decision_id ON event_store_archive (decision_id);
CREATE INDEX IF NOT EXISTS ix_event_store_archive_symbol ON event_store_archive (symbol);
CREATE INDEX IF NOT EXISTS ix_event_store_archive_product_type ON event_store_archive (product_type);
CREATE INDEX IF NOT EXISTS ix_event_store_archive_margin_mode ON event_store_archive (margin_mode);
CREATE INDEX IF NOT EXISTS ix_event_store_archive_topic_scope_seq ON event_store_archive (topic, product_type, margin_mode, source_sequence_id);
CREATE INDEX IF NOT EXISTS ix_event_store_archive_topic_symbol_seq ON event_store_archive (topic, symbol, source_sequence_id);

-- Ensure indexes exist on projection_replay_offsets
CREATE INDEX IF NOT EXISTS ix_projection_replay_offsets_projection ON projection_replay_offsets (projection_key);
CREATE INDEX IF NOT EXISTS ix_projection_replay_offsets_scope_updated ON projection_replay_offsets (product_type, margin_mode, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_projection_replay_offsets_projection_scope ON projection_replay_offsets (projection_key, product_type, margin_mode, allowed_symbols_hash);

-- Ensure indexes exist on execution_orders
CREATE INDEX IF NOT EXISTS ix_execution_orders_allocation_id ON execution_orders (allocation_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_orders_client_order_id ON execution_orders (client_order_id);
CREATE INDEX IF NOT EXISTS ix_execution_orders_decision_created ON execution_orders (decision_id, created_at);
CREATE INDEX IF NOT EXISTS ix_execution_orders_decision_id ON execution_orders (decision_id);
CREATE INDEX IF NOT EXISTS ix_execution_orders_instrument_family ON execution_orders (instrument_family);
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_orders_intent_id ON execution_orders (intent_id);
CREATE INDEX IF NOT EXISTS ix_execution_orders_margin_mode ON execution_orders (margin_mode);
CREATE INDEX IF NOT EXISTS ix_execution_orders_pos_side ON execution_orders (pos_side);
CREATE INDEX IF NOT EXISTS ix_execution_orders_position_intent ON execution_orders (position_intent);
CREATE INDEX IF NOT EXISTS ix_execution_orders_position_mode ON execution_orders (position_mode);
CREATE INDEX IF NOT EXISTS ix_execution_orders_product_type ON execution_orders (product_type);
CREATE INDEX IF NOT EXISTS ix_execution_orders_settle_currency ON execution_orders (settle_currency);
CREATE INDEX IF NOT EXISTS ix_execution_orders_state ON execution_orders (state);
CREATE INDEX IF NOT EXISTS ix_execution_orders_strategy_bundle_id ON execution_orders (strategy_bundle_id);
CREATE INDEX IF NOT EXISTS ix_execution_orders_strategy_family ON execution_orders (strategy_family);
CREATE INDEX IF NOT EXISTS ix_execution_orders_strategy_leg_role ON execution_orders (strategy_leg_role);
CREATE INDEX IF NOT EXISTS ix_execution_orders_strategy_sleeve_id ON execution_orders (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_execution_orders_symbol ON execution_orders (symbol);
CREATE INDEX IF NOT EXISTS ix_execution_orders_symbol_state ON execution_orders (symbol, state);
CREATE INDEX IF NOT EXISTS ix_execution_orders_td_mode ON execution_orders (td_mode);
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_orders_venue_order_id ON execution_orders (venue_order_id);

-- Ensure indexes exist on external_event_inbox
CREATE UNIQUE INDEX IF NOT EXISTS ix_external_event_inbox_dedupe_key ON external_event_inbox (dedupe_key);
CREATE INDEX IF NOT EXISTS ix_external_event_inbox_processing_result ON external_event_inbox (processing_result);
CREATE INDEX IF NOT EXISTS ix_external_event_inbox_source_received ON external_event_inbox (source_system, received_at);
CREATE INDEX IF NOT EXISTS ix_external_event_inbox_source_system ON external_event_inbox (source_system);

-- Ensure indexes exist on fill_events
CREATE INDEX IF NOT EXISTS ix_fill_events_allocation_id ON fill_events (allocation_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_client_order_id ON fill_events (client_order_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_decision_id ON fill_events (decision_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_instrument_family ON fill_events (instrument_family);
CREATE INDEX IF NOT EXISTS ix_fill_events_intent_id ON fill_events (intent_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_margin_mode ON fill_events (margin_mode);
CREATE INDEX IF NOT EXISTS ix_fill_events_pos_side ON fill_events (pos_side);
CREATE INDEX IF NOT EXISTS ix_fill_events_position_intent ON fill_events (position_intent);
CREATE INDEX IF NOT EXISTS ix_fill_events_position_mode ON fill_events (position_mode);
CREATE INDEX IF NOT EXISTS ix_fill_events_product_type ON fill_events (product_type);
CREATE INDEX IF NOT EXISTS ix_fill_events_scope_symbol_ts ON fill_events (product_type, margin_mode, symbol, ingestion_timestamp);
CREATE INDEX IF NOT EXISTS ix_fill_events_settle_currency ON fill_events (settle_currency);
CREATE INDEX IF NOT EXISTS ix_fill_events_strategy_bundle_id ON fill_events (strategy_bundle_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_strategy_family ON fill_events (strategy_family);
CREATE INDEX IF NOT EXISTS ix_fill_events_strategy_leg_role ON fill_events (strategy_leg_role);
CREATE INDEX IF NOT EXISTS ix_fill_events_strategy_sleeve_id ON fill_events (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_fill_events_symbol ON fill_events (symbol);
CREATE INDEX IF NOT EXISTS ix_fill_events_td_mode ON fill_events (td_mode);

-- Ensure indexes exist on fill_outcomes
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_allocation_id ON fill_outcomes (allocation_id);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_created_at ON fill_outcomes (created_at);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_decision_id ON fill_outcomes (decision_id);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_ingestion_timestamp ON fill_outcomes (ingestion_timestamp);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_intent_id ON fill_outcomes (intent_id);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_margin_mode ON fill_outcomes (margin_mode);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_order_id ON fill_outcomes (order_id, created_at);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_position_intent ON fill_outcomes (position_intent);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_product_type ON fill_outcomes (product_type);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_scope_symbol_ts ON fill_outcomes (product_type, margin_mode, symbol, created_at);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_side ON fill_outcomes (side);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_strategy_bundle_id ON fill_outcomes (strategy_bundle_id);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_strategy_family ON fill_outcomes (strategy_family);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_strategy_leg_role ON fill_outcomes (strategy_leg_role);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_strategy_sleeve_id ON fill_outcomes (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_fill_outcomes_symbol ON fill_outcomes (symbol);

-- Ensure indexes exist on funding_fee_records
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_bill_ts ON funding_fee_records (bill_ts);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_bill_type ON funding_fee_records (bill_type);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_created_at ON funding_fee_records (created_at);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_currency ON funding_fee_records (currency);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_currency_ts ON funding_fee_records (currency, bill_ts);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_funding_direction ON funding_fee_records (funding_direction);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_ledger_journal_id ON funding_fee_records (ledger_journal_id);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_ledger_posting_state ON funding_fee_records (ledger_posting_state);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_margin_mode ON funding_fee_records (margin_mode);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_product_type ON funding_fee_records (product_type);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_scope_symbol_ts ON funding_fee_records (product_type, margin_mode, symbol, bill_ts);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_semantic_group ON funding_fee_records (semantic_group);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_sub_type ON funding_fee_records (sub_type);
CREATE INDEX IF NOT EXISTS ix_funding_fee_records_symbol ON funding_fee_records (symbol);

-- Ensure indexes exist on ledger_accounts
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_account_type ON ledger_accounts (account_type);
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_currency ON ledger_accounts (currency);
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_margin_mode ON ledger_accounts (margin_mode);
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_product_type ON ledger_accounts (product_type);
CREATE INDEX IF NOT EXISTS ix_ledger_accounts_symbol ON ledger_accounts (symbol);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_accounts_identity ON ledger_accounts (account_type, currency, product_type, margin_mode, symbol);

-- Ensure indexes exist on ledger_journals
CREATE INDEX IF NOT EXISTS ix_ledger_journals_source_id ON ledger_journals (source_id);
CREATE INDEX IF NOT EXISTS ix_ledger_journals_source_type ON ledger_journals (source_type);
CREATE INDEX IF NOT EXISTS ix_ledger_journals_status ON ledger_journals (status);
CREATE INDEX IF NOT EXISTS ix_ledger_journals_status_created ON ledger_journals (status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_journals_source ON ledger_journals (source_type, source_id);

-- Ensure indexes exist on operator_users
CREATE INDEX IF NOT EXISTS ix_operator_users_role ON operator_users (role);
CREATE UNIQUE INDEX IF NOT EXISTS ix_operator_users_username ON operator_users (username);

-- Ensure indexes exist on order_obligations
CREATE INDEX IF NOT EXISTS ix_order_obligations_allocation_id ON order_obligations (allocation_id);
CREATE INDEX IF NOT EXISTS ix_order_obligations_currency_status ON order_obligations (reserve_currency, status);
CREATE INDEX IF NOT EXISTS ix_order_obligations_decision_id ON order_obligations (decision_id);
CREATE INDEX IF NOT EXISTS ix_order_obligations_intent_id ON order_obligations (intent_id);
CREATE INDEX IF NOT EXISTS ix_order_obligations_margin_mode ON order_obligations (margin_mode);
CREATE UNIQUE INDEX IF NOT EXISTS ix_order_obligations_obligation_id ON order_obligations (obligation_id);
CREATE INDEX IF NOT EXISTS ix_order_obligations_product_type ON order_obligations (product_type);
CREATE INDEX IF NOT EXISTS ix_order_obligations_reserve_currency ON order_obligations (reserve_currency);
CREATE INDEX IF NOT EXISTS ix_order_obligations_scope_status ON order_obligations (product_type, margin_mode, status);
CREATE INDEX IF NOT EXISTS ix_order_obligations_status ON order_obligations (status);
CREATE INDEX IF NOT EXISTS ix_order_obligations_strategy_bundle_id ON order_obligations (strategy_bundle_id);
CREATE INDEX IF NOT EXISTS ix_order_obligations_strategy_family ON order_obligations (strategy_family);
CREATE INDEX IF NOT EXISTS ix_order_obligations_strategy_leg_role ON order_obligations (strategy_leg_role);
CREATE INDEX IF NOT EXISTS ix_order_obligations_strategy_sleeve_id ON order_obligations (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_order_obligations_symbol ON order_obligations (symbol);

-- Ensure indexes exist on order_states
CREATE INDEX IF NOT EXISTS ix_order_states_allocation_id ON order_states (allocation_id);
CREATE INDEX IF NOT EXISTS ix_order_states_decision_id ON order_states (decision_id);
CREATE INDEX IF NOT EXISTS ix_order_states_instrument_family ON order_states (instrument_family);
CREATE UNIQUE INDEX IF NOT EXISTS ix_order_states_intent_id ON order_states (intent_id);
CREATE INDEX IF NOT EXISTS ix_order_states_margin_mode ON order_states (margin_mode);
CREATE INDEX IF NOT EXISTS ix_order_states_pos_side ON order_states (pos_side);
CREATE INDEX IF NOT EXISTS ix_order_states_position_intent ON order_states (position_intent);
CREATE INDEX IF NOT EXISTS ix_order_states_position_mode ON order_states (position_mode);
CREATE INDEX IF NOT EXISTS ix_order_states_product_type ON order_states (product_type);
CREATE INDEX IF NOT EXISTS ix_order_states_scope_status ON order_states (product_type, margin_mode, status);
CREATE INDEX IF NOT EXISTS ix_order_states_scope_symbol_update ON order_states (product_type, margin_mode, symbol, last_update_ts);
CREATE INDEX IF NOT EXISTS ix_order_states_settle_currency ON order_states (settle_currency);
CREATE INDEX IF NOT EXISTS ix_order_states_status ON order_states (status);
CREATE INDEX IF NOT EXISTS ix_order_states_strategy_bundle_id ON order_states (strategy_bundle_id);
CREATE INDEX IF NOT EXISTS ix_order_states_strategy_family ON order_states (strategy_family);
CREATE INDEX IF NOT EXISTS ix_order_states_strategy_leg_role ON order_states (strategy_leg_role);
CREATE INDEX IF NOT EXISTS ix_order_states_strategy_sleeve_id ON order_states (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_order_states_symbol ON order_states (symbol);
CREATE INDEX IF NOT EXISTS ix_order_states_td_mode ON order_states (td_mode);

-- Ensure indexes exist on outbox_events
CREATE INDEX IF NOT EXISTS ix_outbox_events_created_at ON outbox_events (created_at);
CREATE INDEX IF NOT EXISTS ix_outbox_events_event_key ON outbox_events (event_key);
CREATE INDEX IF NOT EXISTS ix_outbox_events_status ON outbox_events (status);
CREATE INDEX IF NOT EXISTS ix_outbox_events_topic ON outbox_events (topic);
CREATE INDEX IF NOT EXISTS ix_outbox_status_created ON outbox_events (status, created_at);

-- Ensure indexes exist on portfolio_allocation_decisions
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_created_at ON portfolio_allocation_decisions (created_at);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_decision_created ON portfolio_allocation_decisions (decision_id, created_at);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_decision_id ON portfolio_allocation_decisions (decision_id);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_margin_mode ON portfolio_allocation_decisions (margin_mode);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_primary_family ON portfolio_allocation_decisions (primary_family);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_primary_family_created ON portfolio_allocation_decisions (primary_family, created_at);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_primary_strategy_sleeve_id ON portfolio_allocation_decisions (primary_strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_product_type ON portfolio_allocation_decisions (product_type);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_route_action ON portfolio_allocation_decisions (route_action);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_scope_created ON portfolio_allocation_decisions (product_type, margin_mode, created_at);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_symbol ON portfolio_allocation_decisions (symbol);
CREATE INDEX IF NOT EXISTS ix_portfolio_allocation_decisions_symbol_created ON portfolio_allocation_decisions (symbol, created_at);

-- Ensure indexes exist on portfolio_snapshots
CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_margin_mode ON portfolio_snapshots (margin_mode);
CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_primary_symbol ON portfolio_snapshots (primary_symbol);
CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_product_type ON portfolio_snapshots (product_type);
CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_scope_seq ON portfolio_snapshots (product_type, margin_mode, sequence_id);
CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_snapshot_ts ON portfolio_snapshots (snapshot_ts);

-- Ensure indexes exist on position_lots
CREATE INDEX IF NOT EXISTS ix_position_lots_allocation_id ON position_lots (allocation_id);
CREATE INDEX IF NOT EXISTS ix_position_lots_margin_mode ON position_lots (margin_mode);
CREATE INDEX IF NOT EXISTS ix_position_lots_product_type ON position_lots (product_type);
CREATE INDEX IF NOT EXISTS ix_position_lots_scope_symbol_status ON position_lots (product_type, margin_mode, symbol, status);
CREATE INDEX IF NOT EXISTS ix_position_lots_source_fill ON position_lots (source_fill_id);
CREATE INDEX IF NOT EXISTS ix_position_lots_status ON position_lots (status);
CREATE INDEX IF NOT EXISTS ix_position_lots_strategy_sleeve_id ON position_lots (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_position_lots_symbol ON position_lots (symbol);

-- Ensure indexes exist on reconciliation_reports
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_as_of_ts ON reconciliation_reports (as_of_ts);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_decision_id ON reconciliation_reports (decision_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_margin_mode ON reconciliation_reports (margin_mode);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_primary_symbol ON reconciliation_reports (primary_symbol);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_product_type ON reconciliation_reports (product_type);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_scope_ts ON reconciliation_reports (product_type, margin_mode, as_of_ts);
CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_severity ON reconciliation_reports (severity);

-- Ensure indexes exist on reconciliation_findings
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_allocation_id ON reconciliation_findings (allocation_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_bundle_created ON reconciliation_findings (strategy_bundle_id, created_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_created_at ON reconciliation_findings (created_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_finding_type ON reconciliation_findings (finding_type);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_layer ON reconciliation_findings (layer);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_layer_created ON reconciliation_findings (layer, created_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_margin_mode ON reconciliation_findings (margin_mode);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_primary_symbol ON reconciliation_findings (primary_symbol);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_product_type ON reconciliation_findings (product_type);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_reason_code ON reconciliation_findings (reason_code);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_reconciliation_id ON reconciliation_findings (reconciliation_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_recon_created ON reconciliation_findings (reconciliation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_scope_created ON reconciliation_findings (product_type, margin_mode, created_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_scope_kind ON reconciliation_findings (scope_kind);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_scope_ref ON reconciliation_findings (scope_ref);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_severity_class ON reconciliation_findings (severity_class);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_sleeve_created ON reconciliation_findings (strategy_sleeve_id, created_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_strategy_bundle_id ON reconciliation_findings (strategy_bundle_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_findings_strategy_sleeve_id ON reconciliation_findings (strategy_sleeve_id);

-- Ensure indexes exist on baseline_generations
CREATE INDEX IF NOT EXISTS ix_baseline_generations_account_source ON baseline_generations (account_source);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_account_source_imported ON baseline_generations (account_source, imported_at);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_baseline_event_ref ON baseline_generations (baseline_event_ref);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_baseline_id ON baseline_generations (baseline_id);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_baseline_kind ON baseline_generations (baseline_kind);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_exchange_ack_watermark_id ON baseline_generations (exchange_ack_watermark_id);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_imported_at ON baseline_generations (imported_at);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_margin_mode ON baseline_generations (margin_mode);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_operator_action_ref ON baseline_generations (operator_action_ref);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_previous_baseline_ref ON baseline_generations (previous_baseline_ref);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_previous_generation_id ON baseline_generations (previous_generation_id);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_product_type ON baseline_generations (product_type);
CREATE INDEX IF NOT EXISTS ix_baseline_generations_scope_imported ON baseline_generations (product_type, margin_mode, imported_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_baseline_generations_baseline_event_ref ON baseline_generations (baseline_event_ref);

-- Ensure indexes exist on exchange_ack_watermarks
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_account_source ON exchange_ack_watermarks (account_source);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_account_source_ack ON exchange_ack_watermarks (account_source, acknowledged_at);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_acknowledged_at ON exchange_ack_watermarks (acknowledged_at);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_baseline_event_ref ON exchange_ack_watermarks (baseline_event_ref);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_latest_bill_id ON exchange_ack_watermarks (latest_bill_id);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_latest_fill_id ON exchange_ack_watermarks (latest_fill_id);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_latest_reconciliation_id ON exchange_ack_watermarks (latest_reconciliation_id);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_margin_mode ON exchange_ack_watermarks (margin_mode);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_operator_action_ref ON exchange_ack_watermarks (operator_action_ref);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_product_type ON exchange_ack_watermarks (product_type);
CREATE INDEX IF NOT EXISTS ix_exchange_ack_watermarks_scope_ack ON exchange_ack_watermarks (product_type, margin_mode, acknowledged_at);

-- Ensure indexes exist on reconciliation_state_snapshots
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_snapshots_created_at ON reconciliation_state_snapshots (created_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_snapshots_derived_from_generation_id ON reconciliation_state_snapshots (derived_from_generation_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_snapshots_exchange_ack_watermark_id ON reconciliation_state_snapshots (exchange_ack_watermark_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_snapshots_margin_mode ON reconciliation_state_snapshots (margin_mode);
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_snapshots_primary_symbol ON reconciliation_state_snapshots (primary_symbol);
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_snapshots_product_type ON reconciliation_state_snapshots (product_type);
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_snapshots_reconciliation_id ON reconciliation_state_snapshots (reconciliation_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_snapshots_recovery_state ON reconciliation_state_snapshots (recovery_state);
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_scope_created ON reconciliation_state_snapshots (product_type, margin_mode, created_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_state_recovery_created ON reconciliation_state_snapshots (recovery_state, created_at);

-- Ensure indexes exist on sleeve_pnl_records
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_allocation_created ON sleeve_pnl_records (allocation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_allocation_id ON sleeve_pnl_records (allocation_id);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_attribution_type ON sleeve_pnl_records (attribution_type);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_bundle_created ON sleeve_pnl_records (strategy_bundle_id, created_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_created_at ON sleeve_pnl_records (created_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_event_timestamp ON sleeve_pnl_records (event_timestamp);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_event_type ON sleeve_pnl_records (event_type);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_family_created ON sleeve_pnl_records (strategy_family, created_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_fill_id ON sleeve_pnl_records (fill_id);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_funding_fee_id ON sleeve_pnl_records (funding_fee_id);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_margin_mode ON sleeve_pnl_records (margin_mode);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_product_type ON sleeve_pnl_records (product_type);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_scope_symbol_created ON sleeve_pnl_records (product_type, margin_mode, symbol, created_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_sleeve_created ON sleeve_pnl_records (strategy_sleeve_id, created_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_strategy_bundle_id ON sleeve_pnl_records (strategy_bundle_id);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_strategy_family ON sleeve_pnl_records (strategy_family);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_strategy_leg_role ON sleeve_pnl_records (strategy_leg_role);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_strategy_sleeve_id ON sleeve_pnl_records (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_sleeve_pnl_records_symbol ON sleeve_pnl_records (symbol);

-- Ensure indexes exist on strategy_execution_bundles
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_allocation_created ON strategy_execution_bundles (allocation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_allocation_id ON strategy_execution_bundles (allocation_id);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_created_at ON strategy_execution_bundles (created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_decision_created ON strategy_execution_bundles (decision_id, created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_decision_id ON strategy_execution_bundles (decision_id);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_family ON strategy_execution_bundles (family);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_margin_mode ON strategy_execution_bundles (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_product_type ON strategy_execution_bundles (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_route_action ON strategy_execution_bundles (route_action);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_scope_created ON strategy_execution_bundles (product_type, margin_mode, created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_selected_symbol ON strategy_execution_bundles (selected_symbol);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_status ON strategy_execution_bundles (status);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_status_created ON strategy_execution_bundles (status, created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_strategy_sleeve_id ON strategy_execution_bundles (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_strategy_execution_bundles_symbol_created ON strategy_execution_bundles (selected_symbol, created_at);

-- Ensure indexes exist on strategy_profile_activation
CREATE UNIQUE INDEX IF NOT EXISTS ix_strategy_profile_activation_scope ON strategy_profile_activation (product_type, margin_mode, allowed_symbols_hash);

-- Ensure indexes exist on strategy_profile_activation_history
CREATE INDEX IF NOT EXISTS ix_strategy_profile_activation_history_executed_at ON strategy_profile_activation_history (executed_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_activation_history_margin_mode ON strategy_profile_activation_history (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_activation_history_product_type ON strategy_profile_activation_history (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_activation_history_scope_time ON strategy_profile_activation_history (product_type, margin_mode, executed_at);

-- Ensure indexes exist on strategy_profile_evaluations
CREATE INDEX IF NOT EXISTS ix_strategy_profile_evaluations_created_at ON strategy_profile_evaluations (created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_evaluations_margin_mode ON strategy_profile_evaluations (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_evaluations_product_type ON strategy_profile_evaluations (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_evaluations_scope_time ON strategy_profile_evaluations (product_type, margin_mode, created_at);

-- Ensure indexes exist on strategy_profile_recommendations
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_decision_status ON strategy_profile_recommendations (decision_status);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_generated_at ON strategy_profile_recommendations (generated_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_margin_mode ON strategy_profile_recommendations (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_product_type ON strategy_profile_recommendations (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_scope_time ON strategy_profile_recommendations (product_type, margin_mode, generated_at);

-- Ensure indexes exist on strategy_profile_rejections
CREATE INDEX IF NOT EXISTS ix_strategy_profile_rejections_created_at ON strategy_profile_rejections (created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_rejections_margin_mode ON strategy_profile_rejections (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_rejections_product_type ON strategy_profile_rejections (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_rejections_scope_time ON strategy_profile_rejections (product_type, margin_mode, created_at);

-- Ensure indexes exist on strategy_profile_revisions
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_created_at ON strategy_profile_revisions (created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_margin_mode ON strategy_profile_revisions (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_product_type ON strategy_profile_revisions (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_profile_id ON strategy_profile_revisions (profile_id);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_profile_status ON strategy_profile_revisions (profile_id, status);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_scope ON strategy_profile_revisions (product_type, margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_source_recommendation_id ON strategy_profile_revisions (source_recommendation_id);
CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_status ON strategy_profile_revisions (status);

-- Ensure indexes exist on strategy_sleeve_intents
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_allocation_id ON strategy_sleeve_intents (allocation_id);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_created_at ON strategy_sleeve_intents (created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_decision_created ON strategy_sleeve_intents (decision_id, created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_decision_id ON strategy_sleeve_intents (decision_id);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_family ON strategy_sleeve_intents (family);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_inventory_policy ON strategy_sleeve_intents (inventory_policy);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_margin_mode ON strategy_sleeve_intents (margin_mode);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_product_type ON strategy_sleeve_intents (product_type);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_route_action ON strategy_sleeve_intents (route_action);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_scope_created ON strategy_sleeve_intents (product_type, margin_mode, created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_sleeve_created ON strategy_sleeve_intents (strategy_sleeve_id, created_at);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_state ON strategy_sleeve_intents (state);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_strategy_sleeve_id ON strategy_sleeve_intents (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_symbol ON strategy_sleeve_intents (symbol);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_symbol_created ON strategy_sleeve_intents (symbol, created_at);

-- Ensure indexes exist on strategy_sleeves
CREATE INDEX IF NOT EXISTS ix_strategy_sleeves_family ON strategy_sleeves (family);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeves_family_status ON strategy_sleeves (family, status);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeves_inventory_policy ON strategy_sleeves (inventory_policy);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeves_margin_scope ON strategy_sleeves (margin_scope);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeves_product_margin ON strategy_sleeves (product_scope, margin_scope);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeves_product_scope ON strategy_sleeves (product_scope);
CREATE INDEX IF NOT EXISTS ix_strategy_sleeves_status ON strategy_sleeves (status);

-- Ensure indexes exist on sleeve_budget_profiles
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_profiles_family ON sleeve_budget_profiles (family);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_profiles_product_type ON sleeve_budget_profiles (product_type);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_profiles_margin_mode ON sleeve_budget_profiles (margin_mode);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_profiles_scope_updated ON sleeve_budget_profiles (product_type, margin_mode, updated_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_profiles_family_updated ON sleeve_budget_profiles (family, updated_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_profiles_hedge_priority_class ON sleeve_budget_profiles (hedge_priority_class);

-- Ensure indexes exist on sleeve_budget_assignments
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_assignments_budget_profile_id ON sleeve_budget_assignments (budget_profile_id);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_assignments_strategy_sleeve_id ON sleeve_budget_assignments (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_assignments_family ON sleeve_budget_assignments (family);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_assignments_symbol ON sleeve_budget_assignments (symbol);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_assignments_product_type ON sleeve_budget_assignments (product_type);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_assignments_margin_mode ON sleeve_budget_assignments (margin_mode);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_assignments_scope_updated ON sleeve_budget_assignments (product_type, margin_mode, updated_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_assignments_sleeve_updated ON sleeve_budget_assignments (strategy_sleeve_id, updated_at);
CREATE INDEX IF NOT EXISTS ix_sleeve_budget_assignments_hedge_priority_class ON sleeve_budget_assignments (hedge_priority_class);

-- Ensure indexes exist on allocator_budget_snapshots
CREATE INDEX IF NOT EXISTS ix_allocator_budget_snapshots_allocation_id ON allocator_budget_snapshots (allocation_id);
CREATE INDEX IF NOT EXISTS ix_allocator_budget_snapshots_strategy_sleeve_id ON allocator_budget_snapshots (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_allocator_budget_snapshots_symbol ON allocator_budget_snapshots (symbol);
CREATE INDEX IF NOT EXISTS ix_allocator_budget_snapshots_product_type ON allocator_budget_snapshots (product_type);
CREATE INDEX IF NOT EXISTS ix_allocator_budget_snapshots_margin_mode ON allocator_budget_snapshots (margin_mode);
CREATE INDEX IF NOT EXISTS ix_allocator_budget_snapshots_created_at ON allocator_budget_snapshots (created_at);
CREATE INDEX IF NOT EXISTS ix_allocator_budget_snapshots_allocation_created ON allocator_budget_snapshots (allocation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_allocator_budget_snapshots_sleeve_created ON allocator_budget_snapshots (strategy_sleeve_id, created_at);

-- Ensure indexes exist on allocator_conflict_resolutions
CREATE INDEX IF NOT EXISTS ix_allocator_conflict_resolutions_allocation_id ON allocator_conflict_resolutions (allocation_id);
CREATE INDEX IF NOT EXISTS ix_allocator_conflict_resolutions_symbol ON allocator_conflict_resolutions (symbol);
CREATE INDEX IF NOT EXISTS ix_allocator_conflict_resolutions_conflict_type ON allocator_conflict_resolutions (conflict_type);
CREATE INDEX IF NOT EXISTS ix_allocator_conflict_resolutions_resolution_action ON allocator_conflict_resolutions (resolution_action);
CREATE INDEX IF NOT EXISTS ix_allocator_conflict_resolutions_created_at ON allocator_conflict_resolutions (created_at);
CREATE INDEX IF NOT EXISTS ix_allocator_conflict_resolutions_allocation_created ON allocator_conflict_resolutions (allocation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_allocator_conflict_resolutions_symbol_created ON allocator_conflict_resolutions (symbol, created_at);

-- Ensure indexes exist on allocator_netting_decisions
CREATE INDEX IF NOT EXISTS ix_allocator_netting_decisions_allocation_id ON allocator_netting_decisions (allocation_id);
CREATE INDEX IF NOT EXISTS ix_allocator_netting_decisions_symbol ON allocator_netting_decisions (symbol);
CREATE INDEX IF NOT EXISTS ix_allocator_netting_decisions_product_type ON allocator_netting_decisions (product_type);
CREATE INDEX IF NOT EXISTS ix_allocator_netting_decisions_margin_mode ON allocator_netting_decisions (margin_mode);
CREATE INDEX IF NOT EXISTS ix_allocator_netting_decisions_created_at ON allocator_netting_decisions (created_at);
CREATE INDEX IF NOT EXISTS ix_allocator_netting_decisions_allocation_created ON allocator_netting_decisions (allocation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_allocator_netting_decisions_symbol_created ON allocator_netting_decisions (symbol, created_at);

-- Ensure indexes exist on execution_commands
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_commands_idempotency_key ON execution_commands (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_execution_commands_order_id ON execution_commands (order_id);
CREATE INDEX IF NOT EXISTS ix_execution_commands_order_state ON execution_commands (order_id, state);
CREATE INDEX IF NOT EXISTS ix_execution_commands_state ON execution_commands (state);

-- Ensure indexes exist on execution_fills
CREATE INDEX IF NOT EXISTS ix_execution_fills_allocation_id ON execution_fills (allocation_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_client_order_id ON execution_fills (client_order_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_decision_id ON execution_fills (decision_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_instrument_family ON execution_fills (instrument_family);
CREATE INDEX IF NOT EXISTS ix_execution_fills_intent_id ON execution_fills (intent_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_order_id ON execution_fills (order_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_order_ts ON execution_fills (order_id, ingestion_ts);
CREATE INDEX IF NOT EXISTS ix_execution_fills_pos_side ON execution_fills (pos_side);
CREATE INDEX IF NOT EXISTS ix_execution_fills_position_mode ON execution_fills (position_mode);
CREATE INDEX IF NOT EXISTS ix_execution_fills_settle_currency ON execution_fills (settle_currency);
CREATE INDEX IF NOT EXISTS ix_execution_fills_source_system ON execution_fills (source_system);
CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_fills_source_venue_fill ON execution_fills (source_system, venue_fill_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_strategy_bundle_id ON execution_fills (strategy_bundle_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_strategy_family ON execution_fills (strategy_family);
CREATE INDEX IF NOT EXISTS ix_execution_fills_strategy_leg_role ON execution_fills (strategy_leg_role);
CREATE INDEX IF NOT EXISTS ix_execution_fills_strategy_sleeve_id ON execution_fills (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_execution_fills_symbol ON execution_fills (symbol);
CREATE INDEX IF NOT EXISTS ix_execution_fills_symbol_ts ON execution_fills (symbol, ingestion_ts);
CREATE INDEX IF NOT EXISTS ix_execution_fills_td_mode ON execution_fills (td_mode);
CREATE INDEX IF NOT EXISTS ix_execution_fills_venue_order_id ON execution_fills (venue_order_id);

-- Ensure indexes exist on execution_order_state_history
CREATE INDEX IF NOT EXISTS ix_execution_order_state_history_order ON execution_order_state_history (order_id, id);
CREATE INDEX IF NOT EXISTS ix_execution_order_state_history_order_id ON execution_order_state_history (order_id);

-- Ensure indexes exist on ledger_entries
CREATE INDEX IF NOT EXISTS ix_ledger_entries_account_effective ON ledger_entries (account_id, effective_at, created_at);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_account_id ON ledger_entries (account_id);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_currency ON ledger_entries (currency);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_journal ON ledger_entries (journal_id);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_journal_id ON ledger_entries (journal_id);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_source_id ON ledger_entries (source_id);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_source_type ON ledger_entries (source_type);

-- Ensure indexes exist on lot_events
CREATE INDEX IF NOT EXISTS ix_lot_events_allocation_id ON lot_events (allocation_id);
CREATE INDEX IF NOT EXISTS ix_lot_events_event_type ON lot_events (event_type);
CREATE INDEX IF NOT EXISTS ix_lot_events_fill_id ON lot_events (fill_id);
CREATE INDEX IF NOT EXISTS ix_lot_events_lot_id ON lot_events (lot_id);
CREATE INDEX IF NOT EXISTS ix_lot_events_margin_mode ON lot_events (margin_mode);
CREATE INDEX IF NOT EXISTS ix_lot_events_product_type ON lot_events (product_type);
CREATE INDEX IF NOT EXISTS ix_lot_events_scope_symbol ON lot_events (product_type, margin_mode, symbol);
CREATE INDEX IF NOT EXISTS ix_lot_events_strategy_sleeve_id ON lot_events (strategy_sleeve_id);
CREATE INDEX IF NOT EXISTS ix_lot_events_symbol ON lot_events (symbol);

-- Ensure indexes exist on reservations
CREATE UNIQUE INDEX IF NOT EXISTS ix_reservations_order_id ON reservations (order_id);
CREATE INDEX IF NOT EXISTS ix_reservations_reserve_account_id ON reservations (reserve_account_id);
CREATE INDEX IF NOT EXISTS ix_reservations_state ON reservations (state);

-- Ensure indexes exist on settlements
CREATE UNIQUE INDEX IF NOT EXISTS ix_settlements_fill_id ON settlements (fill_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_settlements_journal_id ON settlements (journal_id);
CREATE INDEX IF NOT EXISTS ix_settlements_order_id ON settlements (order_id);
CREATE INDEX IF NOT EXISTS ix_settlements_state ON settlements (state);
