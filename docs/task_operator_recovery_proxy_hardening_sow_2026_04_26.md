# Operator Recovery Proxy Hardening SOW

## Business objectives and boundaries
- Objective: remove stale gateway operator views and misleading recovery action feedback after proxied recovery mutations, and tighten conservative strategy/recovery gates surfaced by review.
- Boundary: no exchange credentials, no schema changes, no deployment changes, no strategy alpha tuning beyond the explicitly reviewed independent thresholds and sizing guard.

## Module responsibilities and domain model
- `OperatorQueryService` owns gateway command proxying and local operator cache invalidation.
- `ReconciliationSystemQueryFacade` owns rebaseline/resume state transitions and human-readable recovery action messages.
- `BlockerActionService` owns blocker panel action feedback.
- Independent strategy sizing/config owns pre-risk-layer target semantics for independent books.
- Stuck submission recovery owns operator-controlled transition of pre-submit local orders to terminal failed state.

## Input/output interfaces
- Existing HTTP and blocker action APIs remain backward compatible.
- Resume/rebaseline responses may include `message`; clients already prefer `result.message` when present.
- Stuck submission resolution response keeps the existing `resolution` payload and tightens eligibility.

## Database schema / tables / indexes / constraints
- No database schema, table, index, or constraint changes.
- Existing event, reconciliation, and order-state persistence paths are reused.

## Transactions, Consistency, Concurrency
- Gateway proxy mutation branches invalidate local TTL caches in `finally` after command invocation.
- Execution-side mutation semantics and transaction boundaries remain unchanged.
- Stuck submission resolution remains single-order and idempotent after the order leaves a pre-submit status.

## Authorization, Authentication, Data Security
- Existing route permissions remain unchanged.
- No secrets, tokens, or live environment files are read or printed.

## Error Handling and Idempotency
- Proxy failures still surface their original exception; gateway cache invalidation is best-effort and local.
- Resume blocked states return explicit operator-facing messages instead of fixed success text.
- Stuck submission self-blocking allowance applies only when reconciliation evidence is limited to the target missing local order.

## State Transition and Lifecycle
- Resume still transitions only through existing recovery states.
- Independent scale-in still requires a live position and now requires a stronger score than entry in the live profile.
- Independent entry sizing remains deterministic but treats balance-aware sizing as a gross family budget split across both books.

## Caching and Performance
- Cache invalidation clears local operator caches for the active scope after proxied mutations.
- No new hot-path queries or polling loops are introduced.

## Logging, Monitoring, Auditing
- Existing operator action, blocker snapshot, and reconciliation auditing remain unchanged.
- Messages are response/UI feedback only and do not replace audit records.

## Testing Strategy
- Unit tests cover proxy cache invalidation, resume blocked messaging, blocker action message passthrough, strict stuck submission self-blocking rejection, and independent sizing/config behavior.
- Existing target position and order semantic duplicate tests are used to confirm prior fixes remain covered where practical.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback is a normal code revert and redeploy through the standard deploy script.

## Configuration and Environment Isolation
- Live independent remains disabled; threshold changes are defensive for future rollout.
- No runtime environment or credential configuration changes.

## Code Organization and Dependencies
- Changes stay within operator recovery, independent sizing/config, and unit tests.
- No new third-party dependency.

## Documentation and Operations Manual
- Operator-facing behavior: resume/rebaseline buttons now report whether automatic running actually resumed or remains blocked.

## Deployment and Acceptance Criteria
- Acceptance: lint passes, full unit tests pass, and the narrowest affected integration test passes in WSL2.
