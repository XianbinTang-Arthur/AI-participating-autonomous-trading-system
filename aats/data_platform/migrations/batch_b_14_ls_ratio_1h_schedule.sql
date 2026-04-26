-- Batch B · Stage 14 — Official 1H long-short ratio collection surface
--
-- The live research DB already contained bronze.market_long_short_ratio_1h
-- from a manual experiment, but no migration, ORM model, or scheduled producer
-- owned it. This stage makes the table an official Bronze surface so freshness
-- audits do not confuse it with an abandoned/orphan table.

BEGIN;

CREATE SCHEMA IF NOT EXISTS bronze;

CREATE TABLE IF NOT EXISTS bronze.market_long_short_ratio_1h (
    symbol                   TEXT         NOT NULL,
    ts                       TIMESTAMPTZ  NOT NULL,
    ls_ratio_positions       NUMERIC(18, 10),
    ls_ratio_accounts        NUMERIC(18, 10),
    ingest_run_id            UUID         NOT NULL,
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);

CREATE INDEX IF NOT EXISTS idx_brz_ls_ratio_1h_ts
    ON bronze.market_long_short_ratio_1h (ts);
CREATE INDEX IF NOT EXISTS idx_brz_ls_ratio_1h_sym_ts
    ON bronze.market_long_short_ratio_1h (symbol, ts);

COMMENT ON TABLE bronze.market_long_short_ratio_1h IS
    'Active RDP collection surface: OKX long-short-account-ratio period=1H. Produced by okx_rest_history_rolling_1h.';

COMMIT;
