CREATE TABLE IF NOT EXISTS exit_execution_intents (
    parent_intent_id VARCHAR(128) PRIMARY KEY,
    execution_chain_id VARCHAR(128) NOT NULL UNIQUE,
    symbol VARCHAR(64) NOT NULL,
    aggregate_status VARCHAR(32) NOT NULL,
    reconciliation_state VARCHAR(32) NOT NULL,
    target_exit_quantity NUMERIC(36, 18) NOT NULL,
    aggregated_filled_quantity NUMERIC(36, 18) NOT NULL DEFAULT 0,
    open_child_working_quantity NUMERIC(36, 18) NOT NULL DEFAULT 0,
    open_child_unknown_quantity NUMERIC(36, 18) NOT NULL DEFAULT 0,
    remaining_dispatchable_quantity NUMERIC(36, 18) NOT NULL DEFAULT 0,
    remaining_unresolved_quantity NUMERIC(36, 18) NOT NULL DEFAULT 0,
    operator_review_required BOOLEAN NOT NULL DEFAULT FALSE,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_exit_execution_intents_chain
    ON exit_execution_intents (execution_chain_id);

CREATE INDEX IF NOT EXISTS ix_exit_execution_intents_symbol_updated
    ON exit_execution_intents (symbol, updated_at);

CREATE INDEX IF NOT EXISTS ix_exit_execution_intents_status_updated
    ON exit_execution_intents (aggregate_status, updated_at);

CREATE TABLE IF NOT EXISTS exit_execution_child_refs (
    client_order_id VARCHAR(128) PRIMARY KEY,
    parent_intent_id VARCHAR(128) NOT NULL REFERENCES exit_execution_intents(parent_intent_id) ON DELETE CASCADE,
    child_order_id VARCHAR(128) NOT NULL,
    exchange_order_id VARCHAR(128),
    execution_chain_id VARCHAR(128),
    intent_id VARCHAR(128),
    symbol VARCHAR(64) NOT NULL,
    planned_quantity NUMERIC(36, 18) NOT NULL,
    known_filled_quantity NUMERIC(36, 18) NOT NULL DEFAULT 0,
    remaining_quantity_estimate NUMERIC(36, 18) NOT NULL DEFAULT 0,
    child_status VARCHAR(32) NOT NULL,
    aggregate_category VARCHAR(32) NOT NULL,
    exchange_truth_pending BOOLEAN NOT NULL DEFAULT FALSE,
    operator_review_required BOOLEAN NOT NULL DEFAULT FALSE,
    risk_reducing_invariant BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_exit_execution_child_refs_parent_updated
    ON exit_execution_child_refs (parent_intent_id, updated_at);

CREATE INDEX IF NOT EXISTS ix_exit_execution_child_refs_chain
    ON exit_execution_child_refs (execution_chain_id);
