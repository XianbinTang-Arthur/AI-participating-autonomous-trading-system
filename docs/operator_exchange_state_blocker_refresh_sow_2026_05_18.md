# Operator Exchange-State Blocker Refresh SOW

Date: 2026-05-18

## Business objectives and boundaries

Make exchange and account readiness blockers actionable from the risk page.
Operators should see a direct refresh action for `okx_system_status_incident`
and `account_state_unready` instead of only generic blocker text.

This change does not bypass safety checks, does not force resume, and does not
mark the account ready. It only exposes the existing backend refresh action and
gives it a frontend request budget suitable for slow account refreshes.

## Module responsibilities and domain model

- `BlockerControlService` classifies active blockers, copy, priority, and
  recommended actions.
- `risk-actions.js` dispatches existing blocker actions from the operator UI.
- `refresh_exchange_state` remains the backend authority for market/account
  refresh and post-refresh blocker evaluation.

## Input/output interfaces

No API schema changes. Existing `/system/blocker-actions/refresh-exchange-state`
payloads remain unchanged.

## Database schema / tables / indexes / constraints

No database changes.

## Transactions, Consistency, Concurrency

No backend transaction changes. The existing frontend in-flight guard still
prevents overlapping operator actions.

## Authorization, Authentication, Data Security

No permission changes. The backend continues to require operator authorization
for blocker actions. No credentials are read or displayed.

## Error Handling and Idempotency

If refresh cannot clear the blocker, the backend still reports that the blocker
remains. The frontend no longer aborts before the normal slow refresh path has
reasonable time to complete.

## State Transition and Lifecycle

The intended loop is:

```text
operator sees exchange/account blocker
  -> refresh exchange state
  -> backend refreshes market/account state and reevaluates blockers
  -> blocker clears or remains with current evidence
```

## Caching and Performance

Only explicit exchange-state refresh actions receive the longer timeout.
Dashboard polling and passive panel requests keep the default request budget.

## Logging, Monitoring, Auditing

Existing operator action records for `refresh_exchange_state` remain the audit
source.

## Testing Strategy

Add unit coverage that OKX system incidents surface the refresh action and that
frontend blocker refresh calls forward the long timeout.

## Migration, Rollback, Compatibility

No migration. Rollback is reverting the new blocker mapping, timeout, and tests.

## Configuration and Environment Isolation

No new environment variables.

## Code Organization and Dependencies

No new dependencies.

## Documentation and Operations Manual

When the risk page reports OKX system status or account readiness blockers,
operators should first use the refresh action. If the blocker remains, the
system is correctly waiting for exchange/account readiness rather than missing
a restore button.

## Deployment and Acceptance Criteria

Acceptance requires targeted blocker/UI tests, ruff, full unit tests, WSL2
targeted integration, standard derivatives-live deploy, and post-deploy health
validation.
