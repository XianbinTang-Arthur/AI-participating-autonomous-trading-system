# Recent No-Order Candidate Drilldown Context Truth

## Business Objectives And Boundaries

Expose a read-only runtime truth surface that links the recent directional no-order decision window to the latest no-order candidate drilldown. The goal is to make the current no-order regime easier to audit without implying that candidate drilldown exists for every historical decision.

This task does not change strategy, risk gates, execution, AI provider behavior, symbol, venue, schema, promotion, release, tuning, or live order behavior.

## Module Responsibilities And Domain Model

`scripts/runtime_truth_report.py` owns the summary. It consumes two existing read-only surfaces:

- `recent_directional_no_order_bridge_decision_context_truth`
- `latest_directional_no_order_candidate_drilldown_truth`

The new surface reports whether the latest drilldown decision matches the recent bridge context, whether the recent window has fills, and whether the drilldown scope remains latest-decision-only.

## Input/Output Interfaces

Input is in-memory runtime truth dictionaries already produced by the runtime truth report. Output is a compact dictionary under `recent_directional_no_order_candidate_drilldown_context_truth` plus flattened fields in `project_live_runtime_facts`.

No raw DB payloads, exchange payloads, secrets, tokens, or connection strings are emitted.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

No writes and no transactions are introduced. Consistency is inherited from the runtime truth snapshot already collected by the report.

## Authorization, Authentication, Data Security

The feature does not add credentials or bypass auth. Existing effective AI runtime auth-gating remains intact. The output explicitly keeps `raw_payload_exposed=false`.

## Error Handling And Idempotency

The summarizer returns a structured status and smallest missing field for missing upstream surfaces, exposed raw payload, latest decision mismatch, or fills in the expected no-order window. The function is deterministic for a given report input.

## State Transition And Lifecycle

No trading state transitions are changed. The surface only classifies the evidence state of the latest no-order decision against the recent no-order context.

## Caching And Performance

The summarizer operates on already-available compact dictionaries and adds no DB queries. Runtime cost is negligible compared with the existing runtime truth generation.

## Logging, Monitoring, Auditing

The surface improves auditing by making the latest-only drilldown scope explicit and flattening the main fields into live runtime facts.

## Testing Strategy

Unit tests cover:

- Verified recent no-order candidate drilldown context.
- Missing latest candidate drilldown classification.
- Flattened `project_live_runtime_facts` exposure.

## Migration, Rollback, Compatibility

Rollback is a code revert of this read-only surface. Existing report fields remain compatible and unchanged.

## Configuration And Environment Isolation

No configuration or environment changes.

## Code Organization And Dependencies

No new dependencies. The implementation follows the existing runtime truth summarizer pattern.

## Documentation And Operations Manual

Operators should read the new surface as evidence about the current/latest decision and the recent no-order window only. Historical per-decision candidate drilldown remains not claimed until a dedicated historical drilldown surface exists.

## Deployment And Acceptance Criteria

Acceptance is binary:

- `recent_directional_no_order_candidate_drilldown_context_truth.status` is `verified_recent_directional_no_order_candidate_drilldown_context` on fresh runtime truth.
- `raw_payload_exposed=false`.
- `historical_candidate_drilldown_scope=latest_decision_only`.
- Focused unit tests, lint, full unit suite, commit, push, deploy, and post-deploy smoke pass.
