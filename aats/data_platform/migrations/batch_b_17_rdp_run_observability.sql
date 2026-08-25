-- Batch B · Stage 17 — RDP logical runs, attempts, steps, and events.
--
-- This migration is additive.  Existing rdp_task_queue rows remain the daemon
-- attempt ledger and are backfilled into one logical run per historical task.

BEGIN;

CREATE TABLE IF NOT EXISTS governance.rdp_runs (
    id                      BIGSERIAL PRIMARY KEY,
    run_id                  VARCHAR(128) NOT NULL,
    workflow                VARCHAR(64) NOT NULL,
    status                  VARCHAR(32) NOT NULL DEFAULT 'queued',
    research_outcome        VARCHAR(64) NOT NULL DEFAULT 'unknown',
    trigger_kind            VARCHAR(32) NOT NULL DEFAULT 'manual',
    requested_by            VARCHAR(128) NOT NULL DEFAULT 'operator',
    idempotency_key         VARCHAR(160),
    source_run_id           VARCHAR(128),
    eligible_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at              TIMESTAMPTZ,
    finished_at             TIMESTAMPTZ,
    heartbeat_at            TIMESTAMPTZ,
    current_step_key        VARCHAR(128),
    completed_steps         INTEGER NOT NULL DEFAULT 0,
    total_steps             INTEGER NOT NULL DEFAULT 0,
    cancel_requested_at     TIMESTAMPTZ,
    error_code              VARCHAR(128),
    error_summary           TEXT,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_rdp_run_id UNIQUE (run_id),
    CONSTRAINT uq_rdp_run_idempotency UNIQUE (idempotency_key),
    CONSTRAINT fk_rdp_run_source FOREIGN KEY (source_run_id)
        REFERENCES governance.rdp_runs(run_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_rdp_run_status CHECK (
        status IN (
            'queued', 'running', 'cancellation_requested', 'succeeded',
            'succeeded_with_warnings', 'partially_succeeded', 'failed', 'cancelled'
        )
    ),
    CONSTRAINT chk_rdp_run_outcome CHECK (
        research_outcome IN (
            'unknown', 'eligible', 'not_eligible', 'inconclusive',
            'blocked_by_data', 'blocked_by_attribution', 'blocked_by_execution'
        )
    ),
    CONSTRAINT chk_rdp_run_trigger CHECK (
        trigger_kind IN ('manual', 'schedule', 'auto_retry', 'recovery')
    ),
    CONSTRAINT chk_rdp_run_step_counts CHECK (
        completed_steps >= 0 AND total_steps >= 0 AND completed_steps <= total_steps
    )
);

CREATE INDEX IF NOT EXISTS ix_rdp_runs_status_created
    ON governance.rdp_runs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rdp_runs_workflow_created
    ON governance.rdp_runs(workflow, created_at DESC);

ALTER TABLE governance.rdp_task_queue
    ADD COLUMN IF NOT EXISTS run_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS attempt_no INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS parent_task_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS trigger_kind VARCHAR(32) NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS priority_class VARCHAR(32) NOT NULL DEFAULT 'normal',
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;

ALTER TABLE governance.rdp_task_queue
    DROP CONSTRAINT IF EXISTS chk_rdp_task_status;

ALTER TABLE governance.rdp_task_queue
    ADD CONSTRAINT chk_rdp_task_status CHECK (
        status IN ('pending', 'running', 'done', 'failed', 'cancelled')
    );

UPDATE governance.rdp_task_queue
SET run_id = task_id,
    trigger_kind = CASE
        WHEN requested_by LIKE 'auto_retry_of_%' THEN 'auto_retry'
        WHEN requested_by = 'scheduler' THEN 'schedule'
        ELSE 'manual'
    END
WHERE run_id IS NULL;

INSERT INTO governance.rdp_runs (
    run_id, workflow, status, trigger_kind, requested_by, eligible_at,
    started_at, finished_at, heartbeat_at, error_code, error_summary,
    created_at, updated_at
)
SELECT
    task_id,
    workflow,
    CASE status
        WHEN 'pending' THEN 'queued'
        WHEN 'running' THEN 'running'
        WHEN 'done' THEN 'succeeded'
        ELSE 'failed'
    END,
    trigger_kind,
    requested_by,
    COALESCE(earliest_start_at, requested_at, created_at),
    started_at,
    finished_at,
    CASE WHEN status = 'running' THEN COALESCE(started_at, requested_at) ELSE NULL END,
    CASE WHEN exit_code = -3 THEN 'worker_orphan_recovered'
         WHEN status = 'failed' THEN 'workflow_failed'
         ELSE NULL END,
    error_message,
    created_at,
    COALESCE(finished_at, started_at, requested_at, created_at)
FROM governance.rdp_task_queue
ON CONFLICT (run_id) DO NOTHING;

ALTER TABLE governance.rdp_task_queue
    ALTER COLUMN run_id SET NOT NULL;

DO $$
BEGIN
    ALTER TABLE governance.rdp_task_queue
        ADD CONSTRAINT fk_rdp_task_run FOREIGN KEY (run_id)
        REFERENCES governance.rdp_runs(run_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE governance.rdp_task_queue
        ADD CONSTRAINT fk_rdp_task_parent FOREIGN KEY (parent_task_id)
        REFERENCES governance.rdp_task_queue(task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE governance.rdp_task_queue
        ADD CONSTRAINT chk_rdp_task_attempt_no CHECK (attempt_no >= 1);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE governance.rdp_task_queue
        ADD CONSTRAINT chk_rdp_task_trigger_kind CHECK (
            trigger_kind IN ('manual', 'schedule', 'auto_retry', 'recovery')
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_rdp_task_run_attempt
    ON governance.rdp_task_queue(run_id, attempt_no DESC);
CREATE INDEX IF NOT EXISTS ix_rdp_task_eligible_priority
    ON governance.rdp_task_queue(status, earliest_start_at, priority_class, created_at);

CREATE TABLE IF NOT EXISTS governance.rdp_run_steps (
    id                  BIGSERIAL PRIMARY KEY,
    step_run_id         VARCHAR(160) NOT NULL,
    run_id              VARCHAR(128) NOT NULL,
    attempt_no          INTEGER NOT NULL,
    step_key            VARCHAR(128) NOT NULL,
    step_order          INTEGER NOT NULL,
    status              VARCHAR(32) NOT NULL DEFAULT 'pending',
    allow_failure       BOOLEAN NOT NULL DEFAULT FALSE,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    exit_code           INTEGER,
    error_code          VARCHAR(128),
    error_summary       TEXT,
    log_ref             TEXT,
    artifact_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_rdp_run_step_id UNIQUE (step_run_id),
    CONSTRAINT uq_rdp_run_attempt_step UNIQUE (run_id, attempt_no, step_key),
    CONSTRAINT fk_rdp_run_step_run FOREIGN KEY (run_id)
        REFERENCES governance.rdp_runs(run_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_rdp_run_step_attempt CHECK (attempt_no >= 1),
    CONSTRAINT chk_rdp_run_step_order CHECK (step_order >= 0),
    CONSTRAINT chk_rdp_run_step_status CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'skipped', 'cancelled')
    )
);

CREATE INDEX IF NOT EXISTS ix_rdp_run_steps_run_order
    ON governance.rdp_run_steps(run_id, attempt_no DESC, step_order);
CREATE INDEX IF NOT EXISTS ix_rdp_run_steps_status
    ON governance.rdp_run_steps(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS governance.rdp_run_events (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              VARCHAR(128) NOT NULL,
    sequence_no         BIGINT NOT NULL,
    attempt_no          INTEGER,
    step_key            VARCHAR(128),
    event_type          VARCHAR(96) NOT NULL,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_rdp_run_event_sequence UNIQUE (run_id, sequence_no),
    CONSTRAINT fk_rdp_run_event_run FOREIGN KEY (run_id)
        REFERENCES governance.rdp_runs(run_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_rdp_run_event_attempt CHECK (attempt_no IS NULL OR attempt_no >= 1)
);

CREATE INDEX IF NOT EXISTS ix_rdp_run_events_run_sequence
    ON governance.rdp_run_events(run_id, sequence_no);
CREATE INDEX IF NOT EXISTS ix_rdp_run_events_occurred
    ON governance.rdp_run_events(occurred_at DESC);

COMMIT;
