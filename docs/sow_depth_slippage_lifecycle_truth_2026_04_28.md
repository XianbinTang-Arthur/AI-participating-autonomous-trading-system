# SOW: Depth-backed slippage lifecycle truth

## Business objectives and boundaries

- Objective: expose a read-only runtime truth surface that links orderbook payload depth readiness with fill/order lifecycle and slippage-cost calibration coverage for OKX `BTC-USDT-SWAP`.
- Boundary: this is an observability/truth-chain change only. It must not change strategy, risk gates, provider behavior, symbol, venue, timeframe plumbing, schema, execution commands, order submission, recovery writes, or manual order behavior.
- Non-goal: do not claim historical fills have sidecar-backed depth attribution unless runtime evidence proves per-fill depth context. Current sidecar readiness may only support future depth-backed calibration.

## Module responsibilities and domain model

- `scripts/runtime_truth_report.py` owns the no-secret runtime truth report and live fact projection.
- Inputs already available:
  - `orderbook_payload_depth_truth`: sidecar payload hash/checksum/exchange-sequence and books5 sequence readiness.
  - `slippage_cost_calibration_truth`: fill fee/slippage reference coverage and command/order reference provenance.
  - `directional_command_flow_provenance_truth`: directional fill coverage by current submit-command versus historical no-submit path.
  - `directional_episode_attribution_truth`: recent directional decision/order/fill/PnL/pretrade microstructure coverage.
- New domain object: `depth_slippage_lifecycle_truth`, a read-only classifier that separates forward depth readiness from actual recent filled directional lifecycle evidence.

## Input/output interfaces

- Input: existing report sections listed above.
- Output: JSON field `depth_slippage_lifecycle_truth` and projected `runtime.live_runtime_facts.*` scalar fields.
- Output must contain only metadata and aggregate counts. It must not include raw orderbook payloads, credentials, or full hashes.

## Database schema / tables / indexes / constraints

- No schema, table, index, or constraint change.
- The report reads existing live/RDP truth surfaces only.

## Transactions, consistency, concurrency

- No writes and no transactions beyond existing read-only probes.
- The classifier is deterministic over one generated runtime truth report.

## Authorization, authentication, data security

- Do not read or print env files.
- Do not expose raw payloads, secrets, tokens, API keys, passwords, or full connection strings.
- Dashboard fields that require auth remain `unknown_auth_required` when unauthenticated.

## Error handling and idempotency

- Missing prerequisites must produce explicit `status` and `smallest_missing_field`.
- Re-running the report is idempotent.

## State transition and lifecycle

- No trading state transitions.
- No order, position, recovery, or manual override state mutation.

## Caching and performance

- No new database queries are required; the classifier consumes existing in-memory report sections.
- Runtime cost is limited to aggregate JSON processing.

## Logging, monitoring, auditing

- Runtime truth and automation artifacts serve as audit evidence.
- Live facts expose scalar readiness/coverage values for operator surfaces and future automation checks.

## Testing strategy

- Unit tests cover verified forward-depth readiness with existing fill baseline and the no-recent-filled-directional-episode blocker.
- Existing focused runtime truth tests must continue to pass.
- Full unit suite must pass before deployment.

## Migration, rollback, compatibility

- Migration: none.
- Rollback: revert the commit and redeploy through `scripts/deploy.sh`.
- Compatibility: additive JSON fields only.

## Configuration and environment isolation

- No config or environment changes.
- Scope remains OKX + `BTC-USDT-SWAP` with `shadow_benchmark=none_verified`.

## Code organization and dependencies

- No new dependencies.
- Keep changes scoped to runtime truth report, tests, and this SOW.

## Documentation and operations manual

- Operators should interpret `forward_depth_ready_no_recent_directional_filled_episode` as: depth and slippage baseline are available, but there is no recent filled directional episode to prove per-episode depth-backed lifecycle attribution.

## Deployment and acceptance criteria

- Acceptance:
  - `depth_slippage_lifecycle_truth` is emitted.
  - Live facts expose status, smallest missing field, depth readiness, slippage sample counts, current command reference coverage, and recent filled directional lifecycle coverage.
  - No raw payload is exposed.
  - Focused and full unit validation pass.
  - Deployment through `scripts/deploy.sh --profile derivatives-live --skip-commit` succeeds.
