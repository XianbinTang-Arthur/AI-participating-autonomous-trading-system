# Operator Resume Action Timeout SOW

Date: 2026-05-18

## Business objectives and boundaries

Keep the operator manual resume action usable when the backend performs slow but
valid recovery work such as account refresh, reconciliation validation, and
resume eligibility checks.

This change only adjusts frontend request timeout budgets for explicit operator
resume actions. It does not weaken backend safety checks, does not force resume,
and does not bypass blockers.

## Module responsibilities and domain model

- `risk-actions.js` owns risk/recovery action dispatch from the operator UI.
- `/system/resume` and `/system/blocker-actions/resume-system` remain backend
  authorities for validation and state transition.

## Input/output interfaces

No API schema changes. Existing POST payloads and responses are unchanged.

## Database schema / tables / indexes / constraints

No database changes.

## Transactions, Consistency, Concurrency

No backend transaction changes. The frontend still uses the existing
`actionInFlight` guard to prevent overlapping operator requests.

## Authorization, Authentication, Data Security

No permission changes. The backend still requires admin access for resume
endpoints.

## Error Handling and Idempotency

Frontend aborts should not fire before the backend's normal resume/rebaseline
proxy budget. Backend failures and blockers are still surfaced as returned
errors.

## State Transition and Lifecycle

Resume remains a backend-validated transition:

```text
operator click -> POST resume endpoint -> backend refresh/reconcile/check -> resumed or blocked
```

## Caching and Performance

Only explicit operator actions get the longer timeout. Dashboard polling and
normal panel requests keep their shorter default timeout.

## Logging, Monitoring, Auditing

No logging changes. Existing backend operator action/audit records remain the
source of truth.

## Testing Strategy

Add frontend action-handler regression coverage that direct resume and blocker
resume actions both forward the extended timeout.

## Migration, Rollback, Compatibility

No migration. Rollback is reverting the timeout override and test.

## Configuration and Environment Isolation

No new environment variables.

## Code Organization and Dependencies

No new dependencies.

## Documentation and Operations Manual

Operators can wait for explicit resume actions to finish without the browser
aborting at the generic 30 second request budget.

## Deployment and Acceptance Criteria

Acceptance requires frontend action tests, relevant operator/blocker tests,
full unit tests, WSL2 targeted integration, and deployment through the standard
WSL2 deploy script.
