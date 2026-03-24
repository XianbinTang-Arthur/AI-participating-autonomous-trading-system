-- AATS PostgreSQL baseline schema
-- Auto-generated from current SQLAlchemy metadata.
-- This file defines the latest supported schema for fresh PostgreSQL deployments.
-- Index creation is deferred to 0002 so legacy upgrades can add columns first.


CREATE TABLE IF NOT EXISTS command_outbox (
	event_id VARCHAR(64) NOT NULL,
	aggregate_type VARCHAR(32) NOT NULL,
	aggregate_id VARCHAR(64) NOT NULL,
	topic VARCHAR(128) NOT NULL,
	payload JSON NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempt_count INTEGER NOT NULL,
	last_error TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (event_id)
);


CREATE TABLE IF NOT EXISTS decision_audit_records (
	audit_revision_id SERIAL NOT NULL,
	decision_id VARCHAR(64) NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	selected_strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	decision_context_ref VARCHAR(64) NOT NULL,
	strategy_coordinator_snapshot_ref VARCHAR(64),
	strategy_sleeve_intent_refs JSON NOT NULL,
	portfolio_allocation_decision_ref VARCHAR(64),
	baseline_assessment_ref VARCHAR(64),
	ai_decision_brief_ref VARCHAR(64),
	ai_market_assessment_ref VARCHAR(64),
	ai_action_proposal_ref VARCHAR(64),
	ai_shadow_decision_refs JSON NOT NULL,
	ai_shadow_evaluation_refs JSON NOT NULL,
	position_target_ref VARCHAR(64),
	decision_outcome_ref VARCHAR(64),
	policy_decision_ref VARCHAR(64),
	risk_decision_ref VARCHAR(64),
	execution_plan_ref VARCHAR(64),
	execution_plan_refs JSON NOT NULL,
	strategy_execution_bundle_ref VARCHAR(64),
	order_intent_refs JSON NOT NULL,
	order_state_refs JSON NOT NULL,
	fill_event_refs JSON NOT NULL,
	portfolio_delta_ref VARCHAR(64),
	reconciliation_refs JSON NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (audit_revision_id)
);


CREATE TABLE IF NOT EXISTS event_store (
	sequence_id SERIAL NOT NULL,
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
	payload JSON NOT NULL,
	PRIMARY KEY (sequence_id)
);


CREATE TABLE IF NOT EXISTS execution_orders (
	order_id VARCHAR(64) NOT NULL,
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
	reduce_only BOOLEAN NOT NULL,
	close_only BOOLEAN NOT NULL,
	td_mode VARCHAR(16),
	position_mode VARCHAR(32),
	pos_side VARCHAR(16),
	reduce_only_reason VARCHAR(64),
	close_only_reason VARCHAR(64),
	instrument_family VARCHAR(64),
	settle_currency VARCHAR(16),
	strategy_family VARCHAR(32),
	strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	strategy_bundle_id VARCHAR(64),
	strategy_leg_role VARCHAR(32),
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	execution_action VARCHAR(32),
	position_intent VARCHAR(32),
	state VARCHAR(32) NOT NULL,
	state_version INTEGER NOT NULL,
	source_system VARCHAR(32) NOT NULL,
	last_exchange_ts TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	raw_payload JSON NOT NULL,
	PRIMARY KEY (order_id)
);


CREATE TABLE IF NOT EXISTS external_event_inbox (
	inbox_id VARCHAR(64) NOT NULL,
	source_system VARCHAR(32) NOT NULL,
	dedupe_key VARCHAR(256) NOT NULL,
	payload JSON NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	processed_at TIMESTAMP WITH TIME ZONE,
	processing_result VARCHAR(16),
	last_error TEXT,
	PRIMARY KEY (inbox_id)
);


CREATE TABLE IF NOT EXISTS fill_events (
	fill_id VARCHAR(64) NOT NULL,
	decision_id VARCHAR(64) NOT NULL,
	intent_id VARCHAR(64) NOT NULL,
	client_order_id VARCHAR(64) NOT NULL,
	exchange_order_id VARCHAR(64) NOT NULL,
	symbol VARCHAR(32) NOT NULL,
	side VARCHAR(8) NOT NULL,
	fill_qty NUMERIC(36, 18) NOT NULL,
	fill_price NUMERIC(36, 18) NOT NULL,
	fee_amount NUMERIC(36, 18) NOT NULL,
	reduce_only BOOLEAN NOT NULL,
	close_only BOOLEAN NOT NULL,
	td_mode VARCHAR(16),
	position_mode VARCHAR(32),
	pos_side VARCHAR(16),
	reduce_only_reason VARCHAR(64),
	close_only_reason VARCHAR(64),
	instrument_family VARCHAR(64),
	settle_currency VARCHAR(16),
	strategy_family VARCHAR(32),
	strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	strategy_bundle_id VARCHAR(64),
	strategy_leg_role VARCHAR(32),
	product_type VARCHAR(16),
	margin_mode VARCHAR(16),
	position_intent VARCHAR(32),
	exchange_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
	ingestion_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (fill_id)
);


CREATE TABLE IF NOT EXISTS fill_outcomes (
	fill_id VARCHAR(64) NOT NULL,
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
	exchange_timestamp TIMESTAMP WITH TIME ZONE,
	ingestion_timestamp TIMESTAMP WITH TIME ZONE,
	order_status_after_fill VARCHAR(32),
	strategy_family VARCHAR(32),
	strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	strategy_bundle_id VARCHAR(64),
	strategy_leg_role VARCHAR(32),
	target_leverage FLOAT,
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
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (fill_id)
);


CREATE TABLE IF NOT EXISTS funding_fee_records (
	bill_id VARCHAR(64) NOT NULL,
	symbol VARCHAR(64),
	currency VARCHAR(16) NOT NULL,
	amount NUMERIC(36, 18) NOT NULL,
	balance_after NUMERIC(36, 18),
	bill_type VARCHAR(16) NOT NULL,
	sub_type VARCHAR(16) NOT NULL,
	type_label VARCHAR(64) NOT NULL,
	sub_type_label VARCHAR(64) NOT NULL,
	semantic_group VARCHAR(32) NOT NULL,
	funding_direction VARCHAR(16) NOT NULL,
	bill_ts TIMESTAMP WITH TIME ZONE,
	ledger_posting_state VARCHAR(16) NOT NULL,
	ledger_journal_id VARCHAR(64),
	ledger_posted_at TIMESTAMP WITH TIME ZONE,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (bill_id)
);


CREATE TABLE IF NOT EXISTS ledger_accounts (
	account_id VARCHAR(64) NOT NULL,
	account_type VARCHAR(32) NOT NULL,
	currency VARCHAR(16) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	symbol VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (account_id)
);


CREATE TABLE IF NOT EXISTS ledger_journals (
	journal_id VARCHAR(64) NOT NULL,
	journal_type VARCHAR(32) NOT NULL,
	source_type VARCHAR(32) NOT NULL,
	source_id VARCHAR(64) NOT NULL,
	status VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	posted_at TIMESTAMP WITH TIME ZONE,
	metadata JSON NOT NULL,
	PRIMARY KEY (journal_id)
);


CREATE TABLE IF NOT EXISTS operator_users (
	user_id VARCHAR(64) NOT NULL,
	username VARCHAR(128) NOT NULL,
	password_hash VARCHAR(512) NOT NULL,
	role VARCHAR(16) NOT NULL,
	enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_login_at TIMESTAMP WITH TIME ZONE,
	payload JSON NOT NULL,
	PRIMARY KEY (user_id)
);


CREATE TABLE IF NOT EXISTS order_obligations (
	client_order_id VARCHAR(64) NOT NULL,
	obligation_id VARCHAR(64) NOT NULL,
	decision_id VARCHAR(64) NOT NULL,
	intent_id VARCHAR(64) NOT NULL,
	symbol VARCHAR(32) NOT NULL,
	reserve_currency VARCHAR(16) NOT NULL,
	status VARCHAR(32) NOT NULL,
	reserved_amount NUMERIC(36, 18) NOT NULL,
	consumed_amount NUMERIC(36, 18) NOT NULL,
	released_amount NUMERIC(36, 18) NOT NULL,
	strategy_family VARCHAR(32),
	strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	strategy_bundle_id VARCHAR(64),
	strategy_leg_role VARCHAR(32),
	product_type VARCHAR(16),
	margin_mode VARCHAR(16),
	last_update_ts TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (client_order_id)
);


CREATE TABLE IF NOT EXISTS order_states (
	client_order_id VARCHAR(64) NOT NULL,
	decision_id VARCHAR(64) NOT NULL,
	intent_id VARCHAR(64) NOT NULL,
	symbol VARCHAR(32) NOT NULL,
	exchange_order_id VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(64) NOT NULL,
	submitted_ts TIMESTAMP WITH TIME ZONE,
	last_update_ts TIMESTAMP WITH TIME ZONE,
	requested_qty NUMERIC(36, 18) NOT NULL,
	filled_qty NUMERIC(36, 18) NOT NULL,
	remaining_qty NUMERIC(36, 18) NOT NULL,
	average_fill_price NUMERIC(36, 18),
	fees NUMERIC(36, 18) NOT NULL,
	reduce_only BOOLEAN NOT NULL,
	close_only BOOLEAN NOT NULL,
	td_mode VARCHAR(16),
	position_mode VARCHAR(32),
	pos_side VARCHAR(16),
	reduce_only_reason VARCHAR(64),
	close_only_reason VARCHAR(64),
	instrument_family VARCHAR(64),
	settle_currency VARCHAR(16),
	strategy_family VARCHAR(32),
	strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	strategy_bundle_id VARCHAR(64),
	strategy_leg_role VARCHAR(32),
	product_type VARCHAR(16),
	margin_mode VARCHAR(16),
	position_intent VARCHAR(32),
	payload JSON NOT NULL,
	PRIMARY KEY (client_order_id)
);


CREATE TABLE IF NOT EXISTS outbox_events (
	event_id VARCHAR(64) NOT NULL,
	topic VARCHAR(128) NOT NULL,
	event_key VARCHAR(128) NOT NULL,
	source_component VARCHAR(128) NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempt_count INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	last_error VARCHAR(512),
	payload JSON NOT NULL,
	PRIMARY KEY (event_id)
);


CREATE TABLE IF NOT EXISTS portfolio_allocation_decisions (
	allocation_id VARCHAR(64) NOT NULL,
	decision_id VARCHAR(64) NOT NULL,
	symbol VARCHAR(64) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	allocator_version VARCHAR(64) NOT NULL,
	automatic_enabled BOOLEAN NOT NULL,
	route_action VARCHAR(32) NOT NULL,
	primary_family VARCHAR(32) NOT NULL,
	primary_strategy_sleeve_id VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (allocation_id)
);


CREATE TABLE IF NOT EXISTS portfolio_snapshots (
	sequence_id SERIAL NOT NULL,
	snapshot_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	total_equity NUMERIC(36, 18) NOT NULL,
	realized_pnl NUMERIC(36, 18) NOT NULL,
	unrealized_pnl NUMERIC(36, 18) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	primary_symbol VARCHAR(64),
	payload JSON NOT NULL,
	PRIMARY KEY (sequence_id)
);


CREATE TABLE IF NOT EXISTS position_lots (
	lot_id VARCHAR(64) NOT NULL,
	symbol VARCHAR(64) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	signed_quantity_open NUMERIC(36, 18) NOT NULL,
	entry_price NUMERIC(36, 18) NOT NULL,
	source_fill_id VARCHAR(64) NOT NULL,
	strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	target_leverage FLOAT NOT NULL,
	exposure_side VARCHAR(16) NOT NULL,
	status VARCHAR(16) NOT NULL,
	opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
	closed_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	metadata JSON NOT NULL,
	PRIMARY KEY (lot_id)
);


CREATE TABLE IF NOT EXISTS reconciliation_reports (
	reconciliation_id VARCHAR(64) NOT NULL,
	decision_id VARCHAR(64),
	as_of_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	severity VARCHAR(32) NOT NULL,
	halt_required BOOLEAN NOT NULL,
	product_type VARCHAR(16),
	margin_mode VARCHAR(16),
	primary_symbol VARCHAR(64),
	payload JSON NOT NULL,
	PRIMARY KEY (reconciliation_id)
);
CREATE TABLE IF NOT EXISTS sleeve_pnl_records (
	record_id VARCHAR(96) NOT NULL,
	strategy_sleeve_id VARCHAR(64),
	strategy_family VARCHAR(32),
	allocation_id VARCHAR(64),
	strategy_bundle_id VARCHAR(64),
	strategy_leg_role VARCHAR(32),
	symbol VARCHAR(64),
	event_type VARCHAR(32) NOT NULL,
	fill_id VARCHAR(64),
	funding_fee_id VARCHAR(64),
	fee_currency VARCHAR(16),
	realized_pnl NUMERIC(36, 18) NOT NULL,
	fee_amount NUMERIC(36, 18) NOT NULL,
	funding_fee_amount NUMERIC(36, 18) NOT NULL,
	inventory_move_qty NUMERIC(36, 18) NOT NULL,
	attribution_type VARCHAR(32) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	event_timestamp TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (record_id)
);


CREATE TABLE IF NOT EXISTS strategy_execution_bundles (
	bundle_id VARCHAR(64) NOT NULL,
	decision_id VARCHAR(64) NOT NULL,
	family VARCHAR(32) NOT NULL,
	strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	route_action VARCHAR(32) NOT NULL,
	status VARCHAR(32) NOT NULL,
	selected_symbol VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (bundle_id)
);


CREATE TABLE IF NOT EXISTS strategy_profile_activation (
	activation_id VARCHAR(64) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	allowed_symbols_hash VARCHAR(64) NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (activation_id)
);


CREATE TABLE IF NOT EXISTS strategy_profile_activation_history (
	activation_event_id VARCHAR(64) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	executed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (activation_event_id)
);


CREATE TABLE IF NOT EXISTS strategy_profile_evaluations (
	evaluation_id VARCHAR(64) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (evaluation_id)
);


CREATE TABLE IF NOT EXISTS strategy_profile_recommendations (
	recommendation_id VARCHAR(64) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	allowed_symbols_json JSON NOT NULL,
	decision_status VARCHAR(16) NOT NULL,
	generated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (recommendation_id)
);


CREATE TABLE IF NOT EXISTS strategy_profile_rejections (
	rejection_id VARCHAR(64) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (rejection_id)
);


CREATE TABLE IF NOT EXISTS strategy_profile_revisions (
	revision_id VARCHAR(64) NOT NULL,
	profile_family VARCHAR(32) NOT NULL,
	profile_id VARCHAR(64) NOT NULL,
	profile_label VARCHAR(128) NOT NULL,
	version INTEGER NOT NULL,
	status VARCHAR(32) NOT NULL,
	risk_level VARCHAR(16) NOT NULL,
	market_intent VARCHAR(32) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	allowed_symbols_json JSON NOT NULL,
	hot_safe_only BOOLEAN NOT NULL,
	auto_switch_allowed BOOLEAN NOT NULL,
	manual_approval_required BOOLEAN NOT NULL,
	payload_json JSON NOT NULL,
	guardrails_json JSON NOT NULL,
	description VARCHAR(512),
	expected_behavior_json JSON NOT NULL,
	created_by VARCHAR(128) NOT NULL,
	created_reason VARCHAR(64) NOT NULL,
	source_recommendation_id VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (revision_id)
);


CREATE TABLE IF NOT EXISTS strategy_sleeve_intents (
	sleeve_intent_id VARCHAR(64) NOT NULL,
	decision_id VARCHAR(64) NOT NULL,
	family VARCHAR(32) NOT NULL,
	strategy_sleeve_id VARCHAR(64) NOT NULL,
	state VARCHAR(32) NOT NULL,
	symbol VARCHAR(64) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	inventory_policy VARCHAR(32) NOT NULL,
	route_action VARCHAR(32) NOT NULL,
	allocation_id VARCHAR(64),
	automatic_enabled BOOLEAN NOT NULL,
	budget_multiplier NUMERIC(36, 18) NOT NULL,
	allocator_weight NUMERIC(36, 18) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (sleeve_intent_id)
);


CREATE TABLE IF NOT EXISTS strategy_sleeves (
	sleeve_id VARCHAR(64) NOT NULL,
	family VARCHAR(32) NOT NULL,
	name VARCHAR(128) NOT NULL,
	product_scope VARCHAR(16) NOT NULL,
	margin_scope VARCHAR(16) NOT NULL,
	automatic_enabled BOOLEAN NOT NULL,
	inventory_policy VARCHAR(32) NOT NULL,
	status VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (sleeve_id)
);


CREATE TABLE IF NOT EXISTS execution_commands (
	command_id VARCHAR(64) NOT NULL,
	order_id VARCHAR(64) NOT NULL,
	command_type VARCHAR(16) NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	state VARCHAR(16) NOT NULL,
	attempt_count INTEGER NOT NULL,
	last_error TEXT,
	command_payload JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (command_id),
	FOREIGN KEY(order_id) REFERENCES execution_orders (order_id)
);


CREATE TABLE IF NOT EXISTS execution_fills (
	fill_id VARCHAR(64) NOT NULL,
	venue_fill_id VARCHAR(128),
	order_id VARCHAR(64) NOT NULL,
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
	reduce_only BOOLEAN NOT NULL,
	close_only BOOLEAN NOT NULL,
	td_mode VARCHAR(16),
	position_mode VARCHAR(32),
	pos_side VARCHAR(16),
	reduce_only_reason VARCHAR(64),
	close_only_reason VARCHAR(64),
	instrument_family VARCHAR(64),
	settle_currency VARCHAR(16),
	strategy_family VARCHAR(32),
	strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	strategy_bundle_id VARCHAR(64),
	strategy_leg_role VARCHAR(32),
	liquidity_role VARCHAR(16),
	exchange_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	ingestion_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	source_system VARCHAR(32) NOT NULL,
	raw_payload JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (fill_id),
	FOREIGN KEY(order_id) REFERENCES execution_orders (order_id)
);


CREATE TABLE IF NOT EXISTS execution_order_state_history (
	id SERIAL NOT NULL,
	order_id VARCHAR(64) NOT NULL,
	from_state VARCHAR(32),
	to_state VARCHAR(32) NOT NULL,
	reason_code VARCHAR(64),
	source VARCHAR(32) NOT NULL,
	source_message_id VARCHAR(128),
	payload JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(order_id) REFERENCES execution_orders (order_id)
);


CREATE TABLE IF NOT EXISTS ledger_entries (
	entry_id VARCHAR(64) NOT NULL,
	journal_id VARCHAR(64) NOT NULL,
	account_id VARCHAR(64) NOT NULL,
	direction VARCHAR(8) NOT NULL,
	amount NUMERIC(36, 18) NOT NULL,
	currency VARCHAR(16) NOT NULL,
	effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
	source_type VARCHAR(32) NOT NULL,
	source_id VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (entry_id),
	FOREIGN KEY(journal_id) REFERENCES ledger_journals (journal_id),
	FOREIGN KEY(account_id) REFERENCES ledger_accounts (account_id)
);


CREATE TABLE IF NOT EXISTS lot_events (
	event_id VARCHAR(64) NOT NULL,
	fill_id VARCHAR(64) NOT NULL,
	lot_id VARCHAR(64) NOT NULL,
	strategy_sleeve_id VARCHAR(64),
	allocation_id VARCHAR(64),
	symbol VARCHAR(64) NOT NULL,
	product_type VARCHAR(16) NOT NULL,
	margin_mode VARCHAR(16) NOT NULL,
	event_type VARCHAR(16) NOT NULL,
	quantity NUMERIC(36, 18) NOT NULL,
	entry_price NUMERIC(36, 18) NOT NULL,
	exit_price NUMERIC(36, 18),
	realized_pnl_delta NUMERIC(36, 18) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (event_id),
	FOREIGN KEY(lot_id) REFERENCES position_lots (lot_id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS reservations (
	reservation_id VARCHAR(64) NOT NULL,
	order_id VARCHAR(64) NOT NULL,
	reserve_account_id VARCHAR(64) NOT NULL,
	reserved_amount NUMERIC(36, 18) NOT NULL,
	consumed_amount NUMERIC(36, 18) NOT NULL,
	released_amount NUMERIC(36, 18) NOT NULL,
	state VARCHAR(32) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (reservation_id),
	FOREIGN KEY(order_id) REFERENCES execution_orders (order_id),
	FOREIGN KEY(reserve_account_id) REFERENCES ledger_accounts (account_id)
);


CREATE TABLE IF NOT EXISTS settlements (
	settlement_id VARCHAR(64) NOT NULL,
	fill_id VARCHAR(64) NOT NULL,
	order_id VARCHAR(64) NOT NULL,
	journal_id VARCHAR(64),
	state VARCHAR(32) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	posted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (settlement_id),
	FOREIGN KEY(fill_id) REFERENCES execution_fills (fill_id),
	FOREIGN KEY(order_id) REFERENCES execution_orders (order_id),
	FOREIGN KEY(journal_id) REFERENCES ledger_journals (journal_id)
);
