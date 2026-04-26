-- Batch B · Stage 13 rollback — RDP collection modeling hygiene

BEGIN;

DO $$
BEGIN
    IF to_regclass('bronze.market_orderbook_payloads') IS NOT NULL
       AND EXISTS (
           SELECT 1
           FROM pg_constraint
           WHERE conname = 'fk_brz_orderbook_payloads_ingest_run'
       ) THEN
        ALTER TABLE bronze.market_orderbook_payloads
            DROP CONSTRAINT fk_brz_orderbook_payloads_ingest_run;
    END IF;
END $$;

ALTER TABLE staging.raw_liquidations DROP CONSTRAINT IF EXISTS chk_raw_liq_source_scope;
DROP INDEX IF EXISTS staging.ix_raw_liquidations_scope_inst_ts;
ALTER TABLE staging.raw_liquidations DROP COLUMN IF EXISTS source_scope;

ALTER TABLE meta.dataset_manifests DROP CONSTRAINT IF EXISTS chk_dm_domain;
ALTER TABLE meta.dataset_manifests ADD CONSTRAINT chk_dm_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text
    ]));

ALTER TABLE meta.dataset_manifests DROP CONSTRAINT IF EXISTS chk_dm_source;
ALTER TABLE meta.dataset_manifests ADD CONSTRAINT chk_dm_source
    CHECK (source_type = ANY (ARRAY[
        'historical_file'::text,
        'api'::text,
        'derived'::text
    ]));

ALTER TABLE meta.dataset_manifests DROP CONSTRAINT IF EXISTS chk_dm_status;
ALTER TABLE meta.dataset_manifests ADD CONSTRAINT chk_dm_status
    CHECK (status = ANY (ARRAY[
        'active'::text,
        'superseded'::text,
        'building'::text,
        'failed'::text
    ]));

ALTER TABLE meta.raw_source_files DROP CONSTRAINT IF EXISTS chk_rsf_domain;
ALTER TABLE meta.raw_source_files ADD CONSTRAINT chk_rsf_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text
    ]));

ALTER TABLE meta.raw_source_files DROP CONSTRAINT IF EXISTS chk_rsf_source;
ALTER TABLE meta.raw_source_files ADD CONSTRAINT chk_rsf_source
    CHECK (source_type = ANY (ARRAY[
        'historical_file'::text,
        'api_snapshot'::text
    ]));

ALTER TABLE meta.quality_reports DROP CONSTRAINT IF EXISTS chk_qr_domain;
ALTER TABLE meta.quality_reports ADD CONSTRAINT chk_qr_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text
    ]));

COMMIT;
