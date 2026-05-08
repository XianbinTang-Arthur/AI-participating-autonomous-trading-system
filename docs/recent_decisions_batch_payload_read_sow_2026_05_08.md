# Recent Decisions Batch Payload Read SOW - 2026-05-08

## Objective

Reduce the remaining `recentDecisions` dashboard snapshot latency observed after the health/blocker read optimization deploy. This is a read-side batching change only.

## Current Behavior

`_build_recent_decisions()` first loads recent audit records, then resolves each decision's event refs one by one through `payload_by_ref()`. Even though the event store supports `get_many()`, the per-row loop turns one dashboard page into many small event lookups.

## Scope

- Keep the `recentDecisions` response schema unchanged.
- Pre-collect decision context, position target, policy, risk, decision outcome, and sleeve-intent refs for the current page.
- Resolve those refs once through `payloads_by_ref_map()`, then build the existing row payloads from the local map.
- Do not change decision audit persistence, trade logic, risk logic, or detail views.

## Acceptance

- Unit tests prove the recent-decisions page resolves refs in one batch.
- Existing dashboard/operator API tests continue to pass.
- Deployment monitoring should show `recentDecisions` moving below the prior ~0.8-1.2s range without introducing gateway errors.
