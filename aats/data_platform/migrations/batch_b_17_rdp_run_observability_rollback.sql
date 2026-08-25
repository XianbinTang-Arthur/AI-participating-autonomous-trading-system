-- Roll back Batch B · Stage 17 only.

BEGIN;

DROP TABLE IF EXISTS governance.rdp_run_events;
DROP TABLE IF EXISTS governance.rdp_run_steps;

ALTER TABLE governance.rdp_task_queue
    DROP CONSTRAINT IF EXISTS fk_rdp_task_parent,
    DROP CONSTRAINT IF EXISTS fk_rdp_task_run,
    DROP CONSTRAINT IF EXISTS chk_rdp_task_attempt_no,
    DROP CONSTRAINT IF EXISTS chk_rdp_task_trigger_kind,
    DROP CONSTRAINT IF EXISTS chk_rdp_task_status;

-- The legacy queue had no cancelled state. Preserve the terminal failure and
-- audit reason before restoring its narrower status contract.
UPDATE governance.rdp_task_queue
SET status = 'failed',
    error_message = CASE
        WHEN COALESCE(error_message, '') = '' THEN 'cancelled_before_batch_b_17_rollback'
        ELSE error_message
    END
WHERE status = 'cancelled';

ALTER TABLE governance.rdp_task_queue
    ADD CONSTRAINT chk_rdp_task_status CHECK (
        status IN ('pending', 'running', 'done', 'failed')
    );

DROP INDEX IF EXISTS governance.ix_rdp_task_run_attempt;
DROP INDEX IF EXISTS governance.ix_rdp_task_eligible_priority;

ALTER TABLE governance.rdp_task_queue
    DROP COLUMN IF EXISTS cancel_requested_at,
    DROP COLUMN IF EXISTS heartbeat_at,
    DROP COLUMN IF EXISTS priority_class,
    DROP COLUMN IF EXISTS trigger_kind,
    DROP COLUMN IF EXISTS parent_task_id,
    DROP COLUMN IF EXISTS attempt_no,
    DROP COLUMN IF EXISTS run_id;

DROP TABLE IF EXISTS governance.rdp_runs;

COMMIT;
