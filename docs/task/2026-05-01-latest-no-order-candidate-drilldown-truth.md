# Latest No-Order Candidate Drilldown Truth

## Business Objectives And Boundaries

Expose a read-only runtime truth surface for the latest directional no-order decision so operators can verify why no order was expected from final blockers, candidate drilldown, and primary-candidate zero-delta evidence. This does not change strategy, risk, execution, provider, schema, symbol, venue, release, promotion, tuning, timeframe, or live order behavior.

## Module Responsibilities And Domain Model

`scripts/runtime_truth_report.py` remains the only implementation module. It reads already-summarized `database_truth.latest_decision.no_trade_attribution`, compacts candidate drilldown fields, and emits a sanitized truth object.

## Input/Output Interfaces

Input is the in-memory runtime truth report data. Output is `latest_directional_no_order_candidate_drilldown_truth` plus flattened `project_live_runtime_facts` fields. No API contract or database schema changes are introduced.

## Database Schema / Tables / Indexes / Constraints

No database schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

The change is read-only and runs inside the existing runtime truth report flow. It introduces no transactions, locks, writes, or concurrent runtime state changes.

## Authorization, Authentication, Data Security

The surface uses existing report data and must not print credentials, connection strings, raw exchange payloads, or raw decision payloads. It stores only compact blocker and candidate metadata.

## Error Handling And Idempotency

Missing database truth, missing latest decision, missing latest no-order bridge, or missing candidate drilldown produce explicit `status` and `smallest_missing_field` values. Re-running the report is idempotent.

## State Transition And Lifecycle

No trading state transitions are changed. The lifecycle impact is limited to better explaining the latest no-order decision context.

## Caching And Performance

No extra database query is added. The summarizer works on existing compact report data and has negligible CPU cost.

## Logging, Monitoring, Auditing

The new truth surface is part of runtime truth output and the flattened live facts audit surface. It is suitable for automation state snapshots.

## Testing Strategy

Add unit tests for verified latest no-order candidate drilldown context, missing drilldown, and live facts projection.

## Migration, Rollback, Compatibility

Rollback is a code revert of the report and test additions. Existing report fields remain backward compatible.

## Configuration And Environment Isolation

No configuration or environment variable changes.

## Code Organization And Dependencies

Use existing helper functions in `scripts/runtime_truth_report.py`; add no dependencies.

## Documentation And Operations Manual

This document is the bounded task handoff. Operators should interpret this surface as attribution evidence only, not alpha or profitability evidence.

## Deployment And Acceptance Criteria

Acceptance requires focused tests, required validation, deployment through `scripts/deploy.sh` if committed, and post-deploy runtime truth showing the new surface verified without blockers.
