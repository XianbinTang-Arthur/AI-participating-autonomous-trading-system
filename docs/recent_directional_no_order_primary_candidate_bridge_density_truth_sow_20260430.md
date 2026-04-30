# Recent Directional No-Order Primary Candidate Bridge Density Truth SOW

## Business objectives and boundaries
- Objective: keep the OKX BTC-USDT-SWAP trading microscope focused on the no-order decision chain by linking recent portfolio-route no-order root density with the latest directional primary-candidate bridge.
- Boundary: read-only runtime truth reporting only. No strategy, risk, execution, provider, schema, symbol, venue, promotion, tuning, or timeframe behavior changes.
- The surface must not claim historical primary-candidate bridge coverage for every recent decision. Current primary-candidate bridge evidence is latest-decision scoped.

## Module responsibilities and domain model
- `scripts/runtime_truth_report.py` owns the projection.
- `recent_directional_no_order_root_cause_density_truth` describes recent portfolio-route no-order root density.
- `latest_directional_no_order_primary_candidate_bridge_truth` distinguishes the latest portfolio route root from the latest directional primary-candidate root.
- `project_live_runtime_facts` exposes compact operator-safe fields for automation state.

## Input/output interfaces
- Input: in-memory sanitized runtime truth dictionaries already produced by the report.
- Output: `recent_directional_no_order_primary_candidate_bridge_density_truth` plus compact live facts.
- Output must not include credentials, connection strings, or raw exchange payloads.

## Database schema / tables / indexes / constraints
- No schema, table, index, or constraint changes.
- The implementation consumes existing DB-derived runtime truth only.

## Transactions, Consistency, Concurrency
- No writes and no transaction semantics change.
- Consistency is bounded by the existing runtime truth DB snapshot.

## Authorization, Authentication, Data Security
- No new authentication path.
- AI/operator runtime read auth behavior remains unchanged.
- Raw payload exposure remains explicitly false.

## Error Handling and Idempotency
- Missing directional attribution, missing recent root density, missing latest bridge, latest bridge outside the recent window, and non-distinct latest roots surface deterministic statuses and smallest missing fields.
- Re-running the report is idempotent.

## State Transition and Lifecycle
- No lifecycle state transition changes.
- The new surface only explains no-order evidence scope and does not affect order or fill state.

## Caching and Performance
- No extra DB query is required.
- The summary reuses existing in-memory report sections.

## Logging, Monitoring, Auditing
- Runtime truth artifacts remain the audit trail.
- Automation state can use compact live facts without parsing raw payloads.

## Testing Strategy
- Add focused unit tests for verified latest-scope bridge density, missing latest bridge, and live-facts projection.
- Run focused tests, ruff, full unit tests, fresh runtime truth, deploy, and post-deploy smoke.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback is reverting this doc plus the runtime truth report and tests.
- Existing keys remain backward compatible.

## Configuration and Environment Isolation
- No configuration or environment variable changes.
- Works in Windows runtime truth and deployed WSL2 runtime as existing report code.

## Code Organization and Dependencies
- Keep implementation inside `scripts/runtime_truth_report.py`.
- Reuse existing helper functions such as `as_dict`, `as_list`, and `int_or_zero`.
- No new dependencies.

## Documentation and Operations Manual
- Operators should treat this surface as recent portfolio-route no-order density plus latest primary-candidate bridge evidence.
- `historical_primary_candidate_bridge_scope=latest_decision_only` means historical per-decision primary-candidate bridges are not claimed by this surface.

## Deployment and Acceptance Criteria
- AC1: Runtime truth includes `recent_directional_no_order_primary_candidate_bridge_density_truth`.
- AC2: Verified status requires recent no-order root density and latest primary-candidate bridge to both be verified.
- AC3: The latest bridge decision is checked against the recent directional window when recent decision ids are available.
- AC4: The report explicitly marks historical primary-candidate bridge scope as latest-decision-only.
- AC5: Raw payload exposure remains false.
- AC6: Focused tests, ruff, full unit tests, deployment via `scripts/deploy.sh --profile derivatives-live --skip-commit`, and post-deploy runtime truth pass.
