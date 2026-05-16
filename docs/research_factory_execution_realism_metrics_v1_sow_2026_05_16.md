# Research Factory Execution Realism Metrics V1 SOW - 2026-05-16

## Business objectives and boundaries

Connect Research Factory candidate metrics to AATS execution realism evidence so candidate gates can evaluate executable net edge, not only toy baseline IC or return. This work remains research-only and must not mutate active parameters, call OKX write APIs, connect RD-Agent or Qlib runtime, deploy code, or read credential files.

## Module responsibilities and domain model

Phase 4 execution realism owns fillability, slippage, fee, funding, turnover, and cost-adjusted edge summaries. Research Factory owns metric taxonomy, baseline metrics, candidate gates, and experiment artifacts. The bridge maps Phase 4 `execution_cost_summary.json` into `MetricsSnapshot` and can merge it over baseline metrics before candidate generation.

## Input/output interfaces

Inputs are existing `execution_cost_summary.json` artifacts and optional smoke runner `--execution-cost-summary` paths. Outputs are `MetricsSnapshot` fields: `fillable_ratio`, `partial_fill_ratio`, `slippage_bps_mean`, `fee_bps_mean`, `funding_bps_mean`, `turnover`, and `cost_adjusted_edge_bps_mean`.

## Database schema / tables / indexes / constraints

No database schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

No transactional changes. Smoke runner artifact writes remain atomic through `ExperimentRecorder`.

## Authorization, Authentication, Data Security

No authentication changes. The bridge reads only explicit artifact JSON files supplied by path. It does not read `.env`, live runtime config, database secrets, or exchange credentials.

## Error Handling and Idempotency

Missing execution realism fields become explicit missing reasons in `MetricsSnapshot`. Non-numeric, null, or malformed summary fields do not silently pass as metrics. If executable cost-adjusted edge fails the gate, the smoke experiment writes a failure artifact.

## State Transition and Lifecycle

Experiment lifecycle remains `running -> succeeded` when merged metrics pass the candidate gate, or `running -> failed` when executable edge or required evidence fails.

## Caching and Performance

No caching changes. Summary parsing is local JSON processing.

## Logging, Monitoring, Auditing

No new runtime monitoring. The merged metrics and candidate artifact manifest remain the audit record.

## Testing Strategy

Add tests for Phase 4 summary cost-stack fields, Research Factory metric mapping, merged smoke metrics, and gate failure on negative executable edge. Run Research Factory tests, targeted execution realism tests, lint, full unit suite, and WSL2 narrow integration.

## Migration, Rollback, Compatibility

Additive schema fields in `execution_cost_summary.json` are backward compatible. Existing summaries without fee/funding/turnover continue to produce missing reasons.

## Configuration and Environment Isolation

No environment configuration changes. The smoke runner accepts an explicit artifact path and stays under research-only artifact roots for output.

## Code Organization and Dependencies

Reuse existing `aats.data_platform.execution_realism.execution_cost_model` and `aats.data_platform.research_factory.metrics.snapshots`. Do not add dependencies.

## Documentation and Operations Manual

Use `.venv\Scripts\python.exe scripts\rdp_run_research_factory_smoke.py --execution-cost-summary <path> --overwrite` to run the Research Factory smoke loop with executable edge evidence.

## Deployment and Acceptance Criteria

No deployment. Acceptance requires generated execution realism metrics to influence candidate gate outcomes and all validation commands to pass.
