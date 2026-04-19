-- Rollback batch_b_07: 恢复 ingest_runs CHECK 约束为 batch_b_01 的原值
-- 注: 若回滚后仍有 microstructure 行或 daemon trigger 行, 本 rollback 会失败
-- (CHECK 约束重建时会 validate 所有既有行). 需要先 DELETE 冲突行.

BEGIN;

ALTER TABLE meta.ingest_runs DROP CONSTRAINT IF EXISTS chk_ir_domain;
ALTER TABLE meta.ingest_runs ADD CONSTRAINT chk_ir_domain
    CHECK (dataset_domain = ANY (ARRAY[
        'candles'::text,
        'funding'::text
    ]));

ALTER TABLE meta.ingest_runs DROP CONSTRAINT IF EXISTS chk_ir_trigger;
ALTER TABLE meta.ingest_runs ADD CONSTRAINT chk_ir_trigger
    CHECK (trigger_mode = ANY (ARRAY[
        'scheduler'::text,
        'manual'::text,
        'auto_gap_repair'::text
    ]));

COMMIT;
