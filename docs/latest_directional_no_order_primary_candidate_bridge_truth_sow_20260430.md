# Latest Directional No-Order Primary Candidate Bridge Truth SOW

## Business objectives and boundaries
- Objective: increase the OKX BTC-USDT-SWAP trading microscope evidence density by making the latest no-order decision semantics explicit.
- Boundary: read-only runtime truth reporting only. No strategy, risk, execution, provider, schema, symbol, venue, promotion, tuning, or timeframe behavior changes.
- The surface must distinguish portfolio-level route action evidence from the primary directional candidate root cause.

## Module responsibilities and domain model
- `scripts/runtime_truth_report.py` owns runtime truth projection.
- `database_truth.latest_decision.execution_truth_chain` states whether the latest decision expects order/fill surfaces.
- `database_truth.latest_decision.no_trade_attribution` states the current no-trade classification and blockers.
- `primary_family_candidate_truth` states the directional candidate route, execution behavior, zero-delta evidence, and global blocker scope.

## Input/output interfaces
- Input: sanitized runtime truth data already loaded by the runtime truth report.
- Output: `latest_directional_no_order_primary_candidate_bridge_truth` and compact `project_live_runtime_facts` fields.
- Output must never include credentials, connection strings, or raw exchange payloads.

## Database schema / tables / indexes / constraints
- No schema, table, index, or constraint changes.
- The implementation consumes existing DB-derived runtime truth only.

## Transactions, Consistency, Concurrency
- No writes and no transaction semantics change.
- Consistency is bounded by the existing runtime truth DB snapshot.

## Authorization, Authentication, Data Security
- No new authentication path.
- AI/operator runtime read auth behavior remains unchanged.
- Raw payload exposure flag must remain explicit and false.

## Error Handling and Idempotency
- Missing DB, missing latest decision, missing no-trade attribution, and missing primary candidate truth are surfaced with deterministic status and smallest missing field.
- Running the report repeatedly is idempotent.

## State Transition and Lifecycle
- No lifecycle state transition changes.
- The bridge only explains why no order/fill lifecycle is expected for the latest no-order decision.

## Caching and Performance
- No extra DB query is required.
- The summary uses existing in-memory runtime truth dictionaries.

## Logging, Monitoring, Auditing
- The runtime truth artifact becomes the audit trail.
- `project_live_runtime_facts` exposes compact fields for automation state and operator review.

## Testing Strategy
- Add focused unit tests for verified bridge semantics, missing primary candidate truth, and live-facts projection.
- Run focused tests, ruff, full unit tests, fresh runtime truth, deploy, and post-deploy smoke.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback is reverting the runtime truth report and tests/doc commit.
- Existing keys remain backward compatible.

## Configuration and Environment Isolation
- No configuration or environment variable changes.
- Works in Windows runtime truth and deployed WSL2 runtime as existing report code.

## Code Organization and Dependencies
- Keep implementation inside `scripts/runtime_truth_report.py`.
- Reuse existing helper functions such as `as_dict`, `as_list`, and `classify_primary_candidate_no_order_semantics`.
- No new dependencies.

## Documentation and Operations Manual
- This SOW documents the intended behavior and acceptance criteria.
- Operators should use the bridge to avoid conflating `advisory_only` portfolio route evidence with `hold_current` directional candidate zero-delta evidence.

## Deployment and Acceptance Criteria
- AC1: Runtime truth includes `latest_directional_no_order_primary_candidate_bridge_truth`.
- AC2: Current latest no-order decision verifies the bridge when latest order expected is false and primary directional candidate no-order semantics are verified.
- AC3: The report explicitly shows whether portfolio route action and primary candidate route action differ.
- AC4: Raw payload exposure remains false.
- AC5: Focused tests, ruff, full unit tests, deployment via `scripts/deploy.sh --profile derivatives-live --skip-commit`, and post-deploy runtime truth pass.
