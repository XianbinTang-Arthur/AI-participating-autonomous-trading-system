CREATE TABLE IF NOT EXISTS strategy_profile_revisions (
    revision_id VARCHAR(64) PRIMARY KEY,
    profile_family VARCHAR(32) NOT NULL DEFAULT 'strategy_tuning',
    profile_id VARCHAR(64) NOT NULL,
    profile_label VARCHAR(128) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL,
    risk_level VARCHAR(16) NOT NULL,
    market_intent VARCHAR(32) NOT NULL,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    allowed_symbols_json JSONB NOT NULL,
    hot_safe_only BOOLEAN NOT NULL DEFAULT TRUE,
    auto_switch_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    manual_approval_required BOOLEAN NOT NULL DEFAULT TRUE,
    payload_json JSONB NOT NULL,
    guardrails_json JSONB NOT NULL,
    description VARCHAR(512) NULL,
    expected_behavior_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by VARCHAR(128) NOT NULL,
    created_reason VARCHAR(64) NOT NULL,
    source_recommendation_id VARCHAR(64) NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_profile_status
    ON strategy_profile_revisions (profile_id, status);

CREATE INDEX IF NOT EXISTS ix_strategy_profile_revisions_scope
    ON strategy_profile_revisions (product_type, margin_mode);

CREATE TABLE IF NOT EXISTS strategy_profile_activation (
    activation_id VARCHAR(64) PRIMARY KEY,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    allowed_symbols_hash VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_strategy_profile_activation_scope
    ON strategy_profile_activation (product_type, margin_mode, allowed_symbols_hash);

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

CREATE INDEX IF NOT EXISTS ix_strategy_profile_recommendations_scope_time
    ON strategy_profile_recommendations (product_type, margin_mode, generated_at);

CREATE TABLE IF NOT EXISTS strategy_profile_activation_history (
    activation_event_id VARCHAR(64) PRIMARY KEY,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_strategy_profile_activation_history_scope_time
    ON strategy_profile_activation_history (product_type, margin_mode, executed_at);

CREATE TABLE IF NOT EXISTS strategy_profile_rejections (
    rejection_id VARCHAR(64) PRIMARY KEY,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_strategy_profile_rejections_scope_time
    ON strategy_profile_rejections (product_type, margin_mode, created_at);

CREATE TABLE IF NOT EXISTS strategy_profile_evaluations (
    evaluation_id VARCHAR(64) PRIMARY KEY,
    product_type VARCHAR(16) NOT NULL,
    margin_mode VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_strategy_profile_evaluations_scope_time
    ON strategy_profile_evaluations (product_type, margin_mode, created_at);
