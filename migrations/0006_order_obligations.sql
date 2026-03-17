CREATE TABLE IF NOT EXISTS order_obligations (
    client_order_id VARCHAR(64) PRIMARY KEY,
    obligation_id VARCHAR(64) NOT NULL UNIQUE,
    decision_id VARCHAR(64) NOT NULL,
    intent_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    reserve_currency VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL,
    reserved_amount DOUBLE PRECISION NOT NULL,
    consumed_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
    released_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
    product_type VARCHAR(16),
    margin_mode VARCHAR(16),
    last_update_ts TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_order_obligations_scope_status
    ON order_obligations (product_type, margin_mode, status);

CREATE INDEX IF NOT EXISTS ix_order_obligations_currency_status
    ON order_obligations (reserve_currency, status);
