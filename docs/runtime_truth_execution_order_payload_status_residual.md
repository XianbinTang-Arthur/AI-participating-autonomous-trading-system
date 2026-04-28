# Runtime Truth: Execution Order Payload Status Residual

## Business Objectives And Boundaries
Classify historical `execution_orders.raw_payload.status` residuals without changing live trading behavior. The authoritative status remains `execution_orders.state` and `order_states.status`.

## Module Responsibilities And Domain Model
`scripts/runtime_truth_report.py` owns read-only runtime truth projection. It reports persisted order status evidence and classifies whether payload layers can be used as authority.

## Input/Output Interfaces
Input is the existing live DB probe executed from the gateway container environment. Output is `execution_order_payload_status_residual_truth` in runtime truth JSON plus projected `live_runtime_facts` fields.

## Database Schema / Tables / Indexes / Constraints
No schema, index, or constraint changes. The probe reads `execution_orders.raw_payload`, `execution_orders.state`, and related order identifiers.

## Transactions, Consistency, Concurrency
Read-only SQL only. No transaction writes, locks, retries, or reconciliation side effects.

## Authorization, Authentication, Data Security
The probe uses the existing container runtime environment. It does not print database URLs, credentials, tokens, or full connection strings.

## Error Handling And Idempotency
Missing DB truth returns an explicit `missing_database_truth` status. Missing probe coverage returns `missing_execution_order_payload_status_residual_probe`.

## State Transition And Lifecycle
No order state transitions are changed. Historical mismatches are classified as diagnostic residuals unless an open-column/terminal-payload conflict appears.

## Caching And Performance
The probe adds bounded aggregate queries and a latest-row sample. No caching changes.

## Logging, Monitoring, Auditing
The runtime truth artifact now exposes authority, coverage, target-order classification, and consumer-audit notes for operator review.

## Testing Strategy
Unit tests cover residual classification and live runtime fact projection. Runtime truth regeneration validates the live DB path.

## Migration, Rollback, Compatibility
Rollback is reverting the script and tests. Existing runtime truth consumers remain compatible because new fields are additive.

## Configuration And Environment Isolation
No configuration changes. Scope remains OKX `BTC-USDT-SWAP` with `shadow_benchmark=none_verified`.

## Code Organization And Dependencies
No new dependencies. The implementation follows existing runtime truth summarizer patterns.

## Documentation And Operations Manual
Operators should treat `execution_orders.state` and `order_states.status` as authoritative. Top-level `raw_payload.status` is diagnostic and may be stale on historical rows.

## Deployment And Acceptance Criteria
Acceptance requires focused unit tests passing and a regenerated runtime truth report showing the residual classification. Deployment, if performed, must use `scripts/deploy.sh --profile derivatives-live --skip-commit`.
