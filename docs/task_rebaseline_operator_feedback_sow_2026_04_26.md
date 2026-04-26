# Rebaseline Operator Feedback SOW - 2026-04-26

## Business Objectives and Boundaries
- Fix the operator-facing ambiguity after "accept current state as new baseline".
- Do not weaken reconciliation, recovery, or resume safety checks.
- Keep the change limited to cache invalidation and operator feedback.

## Module Responsibilities and Domain Model
- `ReconciliationSystemQueryFacade` owns rebaseline command orchestration and response payloads.
- `BlockerActionService` wraps blocker-panel actions and should report the delegated command outcome accurately.
- Frontend action handlers already display `result.message` when present.

## Input/Output Interfaces
- `POST /system/rebaseline` keeps the existing fields and adds a Chinese `message` field.
- `POST /system/blocker-actions/accept-rebaseline` keeps its schema and returns the rebaseline-derived message.

## Database Schema / Tables / Indexes / Constraints
- No schema changes.

## Transactions, Consistency, Concurrency
- Gateway proxy rebaseline invalidates local query caches in `finally`, so timeout/error paths do not leave stale recovery panels.
- No change to execution-process transaction boundaries.

## Authorization, Authentication, Data Security
- Existing admin authorization is preserved.
- No credential files are read or logged.

## Error Handling and Idempotency
- Failed or timed-out proxied rebaseline calls still clear gateway caches.
- Rebaseline remains idempotent at the operator-command level: it records a new accepted baseline and reruns reconciliation.

## State Transition and Lifecycle
- Rebaseline may still end in `normal_operation`, `review_required`, `resume_blocked`, `only_reduce`, or manual halt.
- Operator copy now distinguishes "baseline accepted" from "system safe to resume".

## Caching and Performance
- Only affected scope caches are invalidated after proxied rebaseline.
- No new hot-path work.

## Logging, Monitoring, Auditing
- Existing operator action audit records remain unchanged.

## Testing Strategy
- Unit-test rebaseline message construction.
- Unit-test blocker action message propagation.
- Unit-test gateway proxy cache invalidation.
- Run lint and unit tests; run the narrow operator API integration test if feasible.

## Migration, Rollback, Compatibility
- Backward compatible response extension.
- Rollback is code-only.

## Configuration and Environment Isolation
- No config changes.

## Code Organization and Dependencies
- No new dependencies.
- Keep helper methods inside `reconciliation_system_queries.py`.

## Documentation and Operations Manual
- This SOW documents the operational behavior: accepting a baseline does not imply resume succeeded.

## Deployment and Acceptance Criteria
- `/system/rebaseline` returns an accurate Chinese message for completed, blocked, and review-required outcomes.
- Gateway does not show stale rebaseline blockers immediately after proxied command completion.
- Tests pass.
