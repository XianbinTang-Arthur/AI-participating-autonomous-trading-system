-- Speed up operator dashboard reads for latest/recent decision audit panels.
-- The created_at value is embedded in the audit payload and is stable across
-- revisions for the same decision; updated_at is intentionally not used here
-- because later reconciliation updates can otherwise promote old decisions.

CREATE INDEX IF NOT EXISTS ix_decision_audit_payload_created_revision
    ON decision_audit_records (((payload ->> 'created_at')) DESC NULLS LAST, audit_revision_id DESC);

CREATE INDEX IF NOT EXISTS ix_decision_audit_decision_revision_desc
    ON decision_audit_records (decision_id, audit_revision_id DESC);
