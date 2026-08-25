-- Batch B · Stage 16 — profit-readiness governance ledgers
--
-- These tables record one-time holdout access and execution-owned parameter
-- activation acknowledgements.  They do not grant live-trading permission.

BEGIN;

CREATE TABLE IF NOT EXISTS governance.research_holdout_access_ledger (
    access_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id VARCHAR(160) NOT NULL,
    holdout_content_fingerprint VARCHAR(80) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    git_commit VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'access_started',
    artifact_path TEXT,
    artifact_sha256 VARCHAR(64),
    result_payload JSONB,
    error_message TEXT,
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_holdout_candidate_fingerprint
        UNIQUE (candidate_id, holdout_content_fingerprint),
    CONSTRAINT ck_holdout_access_status CHECK (
        status IN (
            'access_started', 'evaluated_pass', 'evaluated_fail', 'access_failed'
        )
    ),
    CONSTRAINT ck_holdout_reason_nonempty CHECK (length(btrim(reason)) > 0),
    CONSTRAINT ck_holdout_identity_shape CHECK (
        length(btrim(candidate_id)) > 0
        AND length(btrim(actor)) > 0
        AND holdout_content_fingerprint ~ '^rfseg_[0-9a-f]{64}$'
        AND git_commit ~ '^[0-9a-f]{40,64}$'
        AND (
            artifact_sha256 IS NULL
            OR artifact_sha256 ~ '^[0-9a-f]{64}$'
        )
    ),
    CONSTRAINT ck_holdout_terminal_shape CHECK (
        (
            status = 'access_started'
            AND completed_at IS NULL
            AND artifact_path IS NULL
            AND artifact_sha256 IS NULL
            AND result_payload IS NULL
            AND error_message IS NULL
        )
        OR (
            status IN ('evaluated_pass', 'evaluated_fail')
            AND completed_at IS NOT NULL
            AND artifact_path IS NOT NULL
            AND artifact_sha256 IS NOT NULL
            AND result_payload IS NOT NULL
            AND error_message IS NULL
        )
        OR (
            status = 'access_failed'
            AND completed_at IS NOT NULL
            AND artifact_path IS NULL
            AND artifact_sha256 IS NULL
            AND result_payload IS NULL
            AND length(btrim(error_message)) > 0
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_holdout_access_status_time
    ON governance.research_holdout_access_ledger (status, accessed_at);

CREATE TABLE IF NOT EXISTS governance.parameter_activation_operations (
    operation_id VARCHAR(128) PRIMARY KEY,
    operation_type VARCHAR(16) NOT NULL,
    scope VARCHAR(32) NOT NULL,
    scope_ref VARCHAR(160) NOT NULL,
    generation VARCHAR(128) NOT NULL,
    from_parameter_set_id VARCHAR(128),
    to_parameter_set_id VARCHAR(128),
    payload_sha256 VARCHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'pending',
    expected_process_roles TEXT[] NOT NULL,
    actor VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    error_message TEXT,
    deadline_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    terminal_at TIMESTAMPTZ,
    CONSTRAINT uq_parameter_activation_generation
        UNIQUE (scope, scope_ref, generation),
    CONSTRAINT ck_parameter_activation_operation_type CHECK (
        operation_type IN ('apply', 'rollback')
    ),
    CONSTRAINT ck_parameter_activation_state CHECK (
        state IN (
            'pending', 'preparing', 'prepared', 'committing', 'succeeded',
            'failed', 'rollback_required', 'rolling_back', 'rolled_back'
        )
    ),
    CONSTRAINT ck_parameter_activation_roles_nonempty CHECK (
        cardinality(expected_process_roles) > 0
    ),
    CONSTRAINT ck_parameter_activation_reason_nonempty CHECK (
        length(btrim(reason)) > 0
    ),
    CONSTRAINT ck_parameter_activation_identity_shape CHECK (
        length(btrim(scope)) > 0
        AND length(btrim(scope_ref)) > 0
        AND length(btrim(generation)) > 0
        AND length(btrim(actor)) > 0
        AND payload_sha256 ~ '^[0-9a-f]{64}$'
        AND to_parameter_set_id IS NOT NULL
        AND deadline_at > created_at
    ),
    CONSTRAINT ck_parameter_activation_terminal_shape CHECK (
        (
            state IN ('succeeded', 'failed', 'rolled_back')
            AND terminal_at IS NOT NULL
        )
        OR (
            state NOT IN ('succeeded', 'failed', 'rolled_back')
            AND terminal_at IS NULL
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_parameter_activation_nonterminal_scope
    ON governance.parameter_activation_operations (scope, scope_ref)
    WHERE state IN (
        'pending', 'preparing', 'prepared', 'committing',
        'rollback_required', 'rolling_back'
    );

CREATE INDEX IF NOT EXISTS ix_parameter_activation_state_time
    ON governance.parameter_activation_operations (state, created_at);

CREATE TABLE IF NOT EXISTS governance.parameter_runtime_acks (
    id BIGSERIAL PRIMARY KEY,
    operation_id VARCHAR(128) NOT NULL REFERENCES
        governance.parameter_activation_operations(operation_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    process_role VARCHAR(64) NOT NULL,
    phase VARCHAR(16) NOT NULL,
    generation VARCHAR(128) NOT NULL,
    payload_sha256 VARCHAR(64) NOT NULL,
    ack_status VARCHAR(16) NOT NULL,
    observed_parameter_set_id VARCHAR(128),
    details JSONB,
    error_message TEXT,
    ack_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_parameter_runtime_ack UNIQUE (operation_id, process_role, phase),
    CONSTRAINT ck_parameter_runtime_ack_phase CHECK (
        phase IN ('prepare', 'commit', 'readback', 'rollback')
    ),
    CONSTRAINT ck_parameter_runtime_ack_status CHECK (
        ack_status IN ('accepted', 'rejected', 'mismatch', 'timeout')
    ),
    CONSTRAINT ck_parameter_runtime_ack_identity_shape CHECK (
        length(btrim(process_role)) > 0
        AND length(btrim(generation)) > 0
        AND payload_sha256 ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX IF NOT EXISTS ix_parameter_runtime_ack_operation
    ON governance.parameter_runtime_acks (operation_id, ack_at);

DROP TRIGGER IF EXISTS reject_profit_readiness_holdout_writes
    ON governance.research_holdout_access_ledger;
DROP TRIGGER IF EXISTS reject_profit_readiness_operation_writes
    ON governance.parameter_activation_operations;
DROP TRIGGER IF EXISTS reject_profit_readiness_ack_writes
    ON governance.parameter_runtime_acks;
DROP FUNCTION IF EXISTS governance.reject_profit_readiness_writes();

COMMIT;
