# SOW: Decision edge/cost payload truth fallback

## Business objectives and boundaries

- Objective: close the P1 trading microscope attribution gap where recent directional allocation rows have null `expected_edge_bps` / `expected_cost_bps` columns while the same sanitized allocation payload already persists those fields.
- Boundary: read-only runtime truth surface only. Do not change strategy, risk, execution, provider, symbol, venue, timeframe plumbing, schema, migrations, order submission, recovery writes, or live order behavior.

## Module responsibilities and domain model

- `scripts/runtime_truth_report.py` owns sanitized no-secret runtime truth projection.
- `portfolio_allocation_decisions` remains the decision source.
- Domain fact: column values are preferred, but `portfolio_allocation_decisions.payload.expected_edge_bps` and `payload.expected_cost_bps` are valid fallback truth when columns are null.

## Input/output interfaces

- Input: existing `portfolio_allocation_decisions` rows and their existing JSON payload.
- Output: existing `latest_decision.expected_*` and `directional_episode_attribution_truth.recent_decisions[].expected_*` fields are populated from column or payload fallback.
- Additive output: `expected_edge_bps_source` and `expected_cost_bps_source` identify the provenance.

## Database schema / tables / indexes / constraints

- No schema, table, index, or constraint change.
- No backfill or live DB mutation.

## Transactions, Consistency, Concurrency

- No writes and no transactional state transitions.
- Projection is deterministic for one report snapshot.

## Authorization, Authentication, Data Security

- Do not read or print env files.
- Do not expose raw payload, credentials, tokens, API keys, passwords, or connection strings.
- Payload values are projected only as scalar edge/cost bps and source labels.

## Error Handling and Idempotency

- If neither column nor payload contains edge/cost, the existing missing-field behavior remains.
- Re-running the report is idempotent.

## State Transition and Lifecycle

- No trading state, order state, position lifecycle, recovery, or manual override mutation.

## Caching and Performance

- No new DB query is required. The fallback uses payload already fetched by the runtime truth probe.

## Logging, Monitoring, Auditing

- Runtime truth artifacts serve as audit evidence.
- Source labels make column-vs-payload provenance explicit for operators.

## Testing Strategy

- Focused unit test covers column-null payload-present fallback for latest decision and directional episode attribution.
- Full validation follows repo policy where feasible.

## Migration, Rollback, Compatibility

- Migration: none.
- Rollback: revert this doc, `scripts/runtime_truth_report.py`, and the focused test.
- Compatibility: additive fields only; existing consumers still see the same expected edge/cost keys.

## Configuration and Environment Isolation

- No config or environment changes.
- Scope remains OKX + `BTC-USDT-SWAP`; shadow benchmark stays `none_verified`.

## Code Organization and Dependencies

- No new dependencies.
- Keep helpers local to `scripts/runtime_truth_report.py`.

## Documentation and Operations Manual

- Operators should treat `expected_*_source=portfolio_allocation_decisions.payload.*` as persisted allocation evidence, not inferred economics.
- If both column and payload are missing, the truth-chain gap remains open.

## Deployment and Acceptance Criteria

- Acceptance:
  - Runtime truth no longer reports `decisions_with_edge_cost=0` when recent directional allocation payloads contain edge/cost.
  - Source fields prove column or payload provenance.
  - Focused tests pass.
  - No runtime-affecting behavior changes are introduced.
