-- AATS migration 0003
-- Add explicit execution plan and order update refs to decision audit records.

ALTER TABLE decision_audit_records
    ADD COLUMN IF NOT EXISTS execution_plan_ref VARCHAR(64);

ALTER TABLE decision_audit_records
    ADD COLUMN IF NOT EXISTS order_state_refs JSON;

UPDATE decision_audit_records
SET execution_plan_ref = NULLIF(payload ->> 'execution_plan_ref', '')
WHERE execution_plan_ref IS NULL;

UPDATE decision_audit_records
SET order_state_refs = COALESCE(payload -> 'order_state_refs', '[]'::json)
WHERE order_state_refs IS NULL;
