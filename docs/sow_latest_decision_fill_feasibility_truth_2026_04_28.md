# Latest decision fill feasibility truth SOW

## Business objectives and boundaries

Expose a read-only truth surface that explains whether the latest OKX `BTC-USDT-SWAP` decision should have a fill-feasibility expectation. The boundary is diagnostic only: no strategy tuning, risk change, provider change, symbol/venue expansion, schema migration, order mutation, or execution behavior change.

## Module responsibilities and domain model

`scripts/runtime_truth_report.py` owns runtime aggregation. The domain model links latest decision truth, directional episode attribution, RDP silver orderbook/trade-flow context, and the existing execution-science summary.

## Input/output interfaces

Input is the existing runtime truth probe output: `database_truth.latest_decision`, `directional_episode_attribution_truth.recent_decisions`, and `execution_science_truth`. Output is `latest_decision_fill_feasibility_truth` plus selected `live_runtime_facts` projections.

## Database schema / tables / indexes / constraints

No schema, table, index, or constraint changes. The report reads only existing live DB and RDP probe projections.

## Transactions, consistency, concurrency

No transaction or write path is introduced. The report remains an eventually consistent point-in-time diagnostic snapshot.

## Authorization, authentication, data security

No credentials or secrets are printed. Auth-required dashboard fields stay classified as unknown rather than inferred.

## Error handling and idempotency

Missing DB, latest decision, directional attribution, or pretrade microstructure evidence is represented as an explicit status and `smallest_missing_field`. Re-running the report is idempotent.

## State transition and lifecycle

No live order, position, fill, lifecycle, or recovery state transition is performed.

## Caching and performance

The projection reuses data already loaded by runtime truth and performs only in-memory matching by decision id.

## Logging, monitoring, auditing

The new fields become durable audit evidence in runtime truth artifacts and `live_runtime_facts`.

## Testing strategy

Add unit coverage for no-order decisions where fill feasibility is not applicable but decision-time orderbook/trade-flow context is present. Add projection coverage for the new live facts.

## Migration, rollback, compatibility

Rollback is a normal code revert. Existing report fields remain backward compatible.

## Configuration and environment isolation

No new configuration or environment dependency. Scope remains derivatives-live OKX `BTC-USDT-SWAP`.

## Code organization and dependencies

Implementation stays inside `scripts/runtime_truth_report.py` and unit tests. No new dependency.

## Documentation and operations manual

Operators should read `latest_decision_fill_feasibility_truth.status`: no-order decisions should not be interpreted as OKX order/fill failures when `fill_feasibility_applicable=false`.

## Deployment and acceptance criteria

Acceptance passes when focused tests and full unit tests pass, runtime truth emits the new section, deployed head matches Windows head, and no blocking findings appear post-deploy.
