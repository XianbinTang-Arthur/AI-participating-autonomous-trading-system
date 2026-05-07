-- Live operator read-path indexes.
-- These are additive and idempotent; they do not alter trading state.

CREATE INDEX IF NOT EXISTS ix_reconciliation_reports_scope_ts_snapshot_ref
    ON reconciliation_reports (
        product_type,
        margin_mode,
        as_of_ts DESC,
        reconciliation_id DESC,
        ((payload ->> 'portfolio_snapshot_ref'))
    )
    WHERE (payload ->> 'portfolio_snapshot_ref') IS NOT NULL
      AND (payload ->> 'portfolio_snapshot_ref') <> '';

CREATE INDEX IF NOT EXISTS ix_execution_orders_scope_updated
    ON execution_orders (product_type, margin_mode, updated_at DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_execution_fills_ingestion_desc
    ON execution_fills (ingestion_ts DESC, exchange_ts DESC, fill_id DESC);
