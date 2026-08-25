-- Batch B · Stage 16 rollback — preserve audit rows and disable all writers.
--
-- Destructive table removal is intentionally forbidden for these audit ledgers.
-- Re-applying the forward migration removes these fail-closed triggers.

BEGIN;

CREATE OR REPLACE FUNCTION governance.reject_profit_readiness_writes()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'profit_readiness_governance_writes_disabled_by_rollback';
END;
$$;

DROP TRIGGER IF EXISTS reject_profit_readiness_holdout_writes
    ON governance.research_holdout_access_ledger;
CREATE TRIGGER reject_profit_readiness_holdout_writes
    BEFORE INSERT OR UPDATE OR DELETE
    ON governance.research_holdout_access_ledger
    FOR EACH STATEMENT EXECUTE FUNCTION governance.reject_profit_readiness_writes();

DROP TRIGGER IF EXISTS reject_profit_readiness_operation_writes
    ON governance.parameter_activation_operations;
CREATE TRIGGER reject_profit_readiness_operation_writes
    BEFORE INSERT OR UPDATE OR DELETE
    ON governance.parameter_activation_operations
    FOR EACH STATEMENT EXECUTE FUNCTION governance.reject_profit_readiness_writes();

DROP TRIGGER IF EXISTS reject_profit_readiness_ack_writes
    ON governance.parameter_runtime_acks;
CREATE TRIGGER reject_profit_readiness_ack_writes
    BEFORE INSERT OR UPDATE OR DELETE
    ON governance.parameter_runtime_acks
    FOR EACH STATEMENT EXECUTE FUNCTION governance.reject_profit_readiness_writes();

COMMIT;
