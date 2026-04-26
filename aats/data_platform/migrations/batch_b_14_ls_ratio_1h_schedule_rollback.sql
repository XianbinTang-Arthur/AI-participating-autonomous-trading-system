-- Batch B · Stage 14 rollback — remove official 1H long-short ratio surface.

BEGIN;

DROP TABLE IF EXISTS bronze.market_long_short_ratio_1h;

COMMIT;
