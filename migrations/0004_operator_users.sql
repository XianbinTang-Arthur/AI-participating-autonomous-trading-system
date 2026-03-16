-- AATS migration 0004
-- Persistent operator accounts for database-backed browser/session authentication.

CREATE TABLE IF NOT EXISTS operator_users (
    user_id VARCHAR(64) PRIMARY KEY,
    username VARCHAR(128) NOT NULL UNIQUE,
    password_hash VARCHAR(512) NOT NULL,
    role VARCHAR(16) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    last_login_at TIMESTAMPTZ,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_operator_users_role ON operator_users (role);
CREATE INDEX IF NOT EXISTS ix_operator_users_enabled ON operator_users (enabled);
