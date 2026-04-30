# Depth Slippage No-Order Regime Classification SOW

Generated at: 2026-04-30T02:46:24Z

## Business objectives and boundaries

Classify the current OKX BTC-USDT-SWAP directional no-order expected regime in the runtime truth report so depth/slippage lifecycle readiness is not reported as missing fill evidence when recent directional decisions are explicitly no-order expected.

This is read-only runtime truth projection only. It does not change strategy logic, risk gates, execution paths, provider behavior, symbols, venues, schemas, timeframe plumbing, order behavior, recovery behavior, release gates, or promotion rules.

## Module responsibilities and domain model

`scripts/runtime_truth_report.py` owns the operator truth projection. Directional attribution owns whether recent directional decisions expected an order. Depth/slippage lifecycle truth owns whether order book, slippage, command-reference, and recent filled-episode evidence are present or waiting for a material executable episode.

The domain distinction is:

- executable directional regime without recent filled episode: still missing `directional_episode_attribution.recent_directional_filled_decisions`;
- no-order expected directional regime with complete no-order attribution: forward-ready and waiting for a future executable/fill episode.

## Input/output interfaces

Input is the existing in-memory truth objects passed to `summarize_depth_slippage_lifecycle_truth`: order book payload depth, slippage/cost calibration, directional command flow, and directional attribution.

Output is the existing depth/slippage lifecycle truth object plus explicit no-order regime fields under lifecycle coverage and interpretation. No public API contract is removed.

## Database schema / tables / indexes / constraints

No database schema, table, index, or constraint changes are required. The report uses existing read-only runtime truth evidence.

## Transactions, Consistency, Concurrency

No writes or transactions are introduced. Consistency depends on the existing runtime truth report snapshot. The classification avoids claiming a filled episode when recent decisions are all no-order expected.

## Authorization, Authentication, Data Security

No credentials are read or printed. Existing dashboard authenticated fields remain `unknown_auth_required` when unavailable. The report continues to sanitize raw payload and secret-like text.

## Error Handling and Idempotency

If directional attribution is unavailable or not all recent decisions are no-order expected, existing missing-field behavior is preserved. Re-running the report is idempotent because it only reads live facts and emits a derived JSON artifact.

## State Transition and Lifecycle

This change adds an explicit lifecycle state: `forward_depth_ready_no_order_expected_regime`. It represents a forward-ready microscope state waiting for a future executable directional episode rather than a missing recent fill.

## Caching and Performance

No cache behavior changes. The classification uses fields already present in the runtime truth object and adds no database queries.

## Logging, Monitoring, Auditing

The generated runtime truth artifact records the status, no-order regime coverage, and whether the system is waiting for an executable directional episode. Automation state will reference the post-fix and post-deploy artifacts.

## Testing Strategy

Unit tests cover:

- existing non-no-order regime with no recent fills still reports missing recent filled decisions;
- all-recent no-order expected regime reports forward-ready with no missing field;
- live runtime facts expose the new no-order regime flags.

## Migration, Rollback, Compatibility

No migration is needed. Rollback is `git revert` of the implementation commit followed by the standard derivatives-live deploy script. Existing consumers keep the original fields.

## Configuration and Environment Isolation

No configuration changes. The only live target remains OKX BTC-USDT-SWAP. Shadow benchmark remains `none_verified`.

## Code Organization and Dependencies

Changes stay in `scripts/runtime_truth_report.py` and `tests/unit/scripts/test_runtime_truth_report.py`. No new dependency is added.

## Documentation and Operations Manual

This SOW documents the classification rule. Operators should interpret `forward_depth_ready_no_order_expected_regime` as evidence readiness, not profitability or alpha evidence.

## Deployment and Acceptance Criteria

Deploy only with `bash scripts/deploy.sh --profile derivatives-live --skip-commit` after commit and push.

Acceptance criteria:

- AC1: all-recent no-order expected directional regime produces no depth/slippage missing field;
- AC2: non-no-order no-fill regime still reports `directional_episode_attribution.recent_directional_filled_decisions`;
- AC3: focused tests and full unit tests pass;
- AC4: post-deploy runtime truth is clean and deployed head matches Windows head;
- AC5: no live order behavior, strategy, risk, provider, symbol, venue, schema, or timeframe behavior changes.
