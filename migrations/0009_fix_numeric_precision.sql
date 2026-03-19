ALTER TABLE IF EXISTS portfolio_snapshots
    ALTER COLUMN total_equity TYPE NUMERIC(36, 18) USING total_equity::NUMERIC(36, 18),
    ALTER COLUMN realized_pnl TYPE NUMERIC(36, 18) USING realized_pnl::NUMERIC(36, 18),
    ALTER COLUMN unrealized_pnl TYPE NUMERIC(36, 18) USING unrealized_pnl::NUMERIC(36, 18);

ALTER TABLE IF EXISTS order_states
    ALTER COLUMN requested_qty TYPE NUMERIC(36, 18) USING requested_qty::NUMERIC(36, 18),
    ALTER COLUMN filled_qty TYPE NUMERIC(36, 18) USING filled_qty::NUMERIC(36, 18),
    ALTER COLUMN remaining_qty TYPE NUMERIC(36, 18) USING remaining_qty::NUMERIC(36, 18),
    ALTER COLUMN average_fill_price TYPE NUMERIC(36, 18) USING average_fill_price::NUMERIC(36, 18),
    ALTER COLUMN fees TYPE NUMERIC(36, 18) USING fees::NUMERIC(36, 18);

ALTER TABLE IF EXISTS fill_events
    ALTER COLUMN fill_qty TYPE NUMERIC(36, 18) USING fill_qty::NUMERIC(36, 18),
    ALTER COLUMN fill_price TYPE NUMERIC(36, 18) USING fill_price::NUMERIC(36, 18),
    ALTER COLUMN fee_amount TYPE NUMERIC(36, 18) USING fee_amount::NUMERIC(36, 18);

ALTER TABLE IF EXISTS order_obligations
    ALTER COLUMN reserved_amount TYPE NUMERIC(36, 18) USING reserved_amount::NUMERIC(36, 18),
    ALTER COLUMN consumed_amount TYPE NUMERIC(36, 18) USING consumed_amount::NUMERIC(36, 18),
    ALTER COLUMN released_amount TYPE NUMERIC(36, 18) USING released_amount::NUMERIC(36, 18);
