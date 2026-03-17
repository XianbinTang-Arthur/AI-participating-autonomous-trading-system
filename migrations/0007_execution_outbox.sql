CREATE TABLE IF NOT EXISTS outbox_events (
    event_id VARCHAR(64) PRIMARY KEY,
    topic VARCHAR(128) NOT NULL,
    event_key VARCHAR(128) NOT NULL,
    source_component VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ NULL,
    last_error VARCHAR(512) NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_outbox_status_created
    ON outbox_events (status, created_at);
