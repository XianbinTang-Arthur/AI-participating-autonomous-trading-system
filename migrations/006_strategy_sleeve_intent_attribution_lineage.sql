-- Add immutable lineage needed for exact replay/live attribution.
-- Existing rows remain NULL and are treated as legacy_unattributable; they are
-- never guessed from created_at because that would mix 15m and 1h decisions.

ALTER TABLE strategy_sleeve_intents
    ADD COLUMN IF NOT EXISTS timeframe VARCHAR(8),
    ADD COLUMN IF NOT EXISTS signal_bar_start TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS signal_bar_end TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS market_data_asof TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS parameter_set_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS runtime_generation VARCHAR(128),
    ADD COLUMN IF NOT EXISTS code_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS market_snapshot_ref VARCHAR(128),
    ADD COLUMN IF NOT EXISTS feature_snapshot_ref VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_strategy_sleeve_intents_attribution_lineage
    ON strategy_sleeve_intents (family, symbol, timeframe, signal_bar_start);
