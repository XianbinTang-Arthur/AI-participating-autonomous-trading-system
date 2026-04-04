# Task 211: Remove Independent Recovery Snapshot Card From Risk UI

## Business objectives and boundaries
- Remove the `风险恢复` page card titled `独立双书恢复快照`.
- Keep recovery APIs and backend payloads unchanged.
- Do not change other risk/recovery cards or replay/exit-execution diagnostics.

## Module responsibilities and domain model
- `aats/api/static/modules/views/risk-view.js`
  - Owns risk page section composition and card rendering.
  - This change only removes one read-only presentation card.
- `tests/integration/test_dashboard_ui.py`
  - Verifies rendered dashboard HTML no longer surfaces the removed card.

## Input/output interfaces
- Input remains `systemRecovery.recovery.independent_recovery_snapshots`.
- Output change: the risk page no longer renders a dedicated card from that input.

## Database schema / tables / indexes / constraints
- No database changes.

## Transactions, Consistency, Concurrency
- Not applicable. Frontend-only rendering change.

## Authorization, Authentication, Data Security
- No auth or security behavior changes.

## Error Handling and Idempotency
- Rendering remains idempotent.
- Removing the card must not break when `independent_recovery_snapshots` is present or absent.

## State Transition and Lifecycle
- No backend lifecycle changes.
- UI lifecycle only: one optional section is no longer emitted.

## Caching and Performance
- Slightly less DOM output and less client-side formatting work.
- No caching changes.

## Logging, Monitoring, Auditing
- No logging or audit changes.

## Testing Strategy
- Update dashboard integration coverage so injected `independent_recovery_snapshots` data does not render the removed card.
- Run lint, full unit tests, and the narrowest affected dashboard integration test.

## Migration, Rollback, Compatibility
- Backward compatible at the API layer.
- UI-only removal; rollback is a single-file revert if needed.

## Configuration and Environment Isolation
- No config changes.

## Code Organization and Dependencies
- Keep the change isolated to `risk-view.js` and the affected UI test.
- Avoid touching recovery domain services.

## Documentation and Operations Manual
- This SOW is the only new documentation needed for the removal.

## Deployment and Acceptance Criteria
- Risk page no longer shows `独立双书恢复快照`.
- Existing recovery and exit-execution cards still render normally.
- Validation commands pass.
