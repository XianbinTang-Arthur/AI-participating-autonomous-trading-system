# Execution Science Slippage Reference Price Truth SOW - 2026-04-26

## Business objectives and boundaries
- Objective: close the current slippage proxy truth gap by using the correct read-only reference price source for directional BTC-USDT-SWAP fills.
- Boundary: no strategy, risk, provider, schema, venue, symbol, release, promotion, tuning, timeframe, or live order behavior changes.

## Module responsibilities and domain model
- `scripts/runtime_truth_report.py` owns the no-secret automation truth projection.
- `execution_orders` remains the canonical order lifecycle table.
- `execution_commands.command_payload.intent.reference_price` is submit-time intent evidence and can serve as a read-only market/arrival reference for market-order fill slippage proxy.

## Input/output interfaces
- Input: gateway container environment-loaded live DB URL and RDP DB URL, queried inside the running container without printing credentials.
- Output: runtime truth `slippage_cost_calibration_truth` includes reference-source counts and computes slippage proxy from order or command reference price evidence.

## Database schema / tables / indexes / constraints
- No schema, table, index, or constraint changes.
- The live read-only probe touches existing `execution_fills`, `execution_orders`, and `execution_commands`.

## Transactions, Consistency, Concurrency
- No write transaction is introduced.
- Probe results are point-in-time aggregates and tolerate concurrent fill/order/command writes.

## Authorization, Authentication, Data Security
- Credentials are read only inside the running gateway container process environment.
- The report never emits database URLs, passwords, tokens, raw exchange payloads, client order ids, venue order ids, command ids, or fill ids.

## Error Handling and Idempotency
- Missing reference prices degrade to explicit status and smallest missing field.
- Re-running the report is idempotent for the same runtime state.

## State Transition and Lifecycle
- No live trading state transition is changed.
- Runtime truth only classifies whether realized fee and slippage proxy evidence is available.

## Caching and Performance
- No cache is introduced.
- Queries are bounded aggregates over existing order/fill/command tables.

## Logging, Monitoring, Auditing
- Automation can now distinguish no reference price from reference price present in the command payload but absent from the order row.
- Reference source counts support future audit of execution truth persistence quality.

## Testing Strategy
- Unit tests cover command-payload reference price as a valid slippage reference source and missing-reference degradation.
- Runtime smoke verifies the report against live aggregate data.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback is a normal code revert and redeploy through `scripts/deploy.sh --skip-commit`.

## Configuration and Environment Isolation
- No new configuration is required.
- The symbol remains fixed to BTC-USDT-SWAP for the current stage.

## Code Organization and Dependencies
- Changes stay in `scripts/runtime_truth_report.py` and `tests/unit/scripts/test_runtime_truth_report.py`.
- No new dependency.

## Documentation and Operations Manual
- Slippage proxy reference priority is order limit price, order raw intent/reference price, order-state reference price, then submit command intent limit/reference price.
- For current directional market orders, the observed live source is `execution_commands.command_payload.intent.reference_price`.

## Deployment and Acceptance Criteria
- Acceptance: focused runtime truth tests pass, lint passes, full unit tests pass, deployment via standard script succeeds, and post-deploy runtime truth reports slippage proxy samples greater than zero or an exact source-level blocker.
