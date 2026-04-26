-- Batch B · Stage 12 rollback — Orderbook diff payload sidecar
--
-- Drops only bronze.market_orderbook_payloads and its dependent indexes /
-- constraints. Existing bbo/books5 snapshot rows are not mutated.

BEGIN;

DROP TABLE IF EXISTS bronze.market_orderbook_payloads;

COMMIT;
