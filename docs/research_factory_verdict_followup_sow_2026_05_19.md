# Research Factory Verdict Sprint Follow-up SOW

Date: 2026-05-19

## Business Objectives And Boundaries

Use the existing Research Factory to keep evaluating real candidates and expose
real data/observation bottlenecks. This work does not add new governance
Package, Review, Approval, SafetyPolicy, execution, or apply layers.

The boundary is research-only:

```text
runtime_mutation_allowed = false
active_parameter_write_allowed = false
runtime_config_write_allowed = false
okx_write_allowed = false
dry_run_execution_allowed = false
```

## Module Responsibilities And Domain Model

- `configs/research_factory/clean_windows.json` records reusable known-clean
  Gold replay windows for current Research Factory reruns.
- Research Factory factor evaluation remains responsible for computing safe DSL
  factor values.
- Verdict board remains the stage output for candidates:
  `reject`, `keep_observing`, or `positive_executable_edge`.
- Observation source audit remains documentation-only until a real read-only
  candidate-bound shadow/paper event source exists.

## Input And Output Interfaces

Inputs:

- Existing workflow summaries under `artifacts/research/research_factory/workflows`.
- Gold replay clean-window findings for BTC-USDT-SWAP 1h and 15m.
- Read-only DB schema/count probes for observation source discovery.

Outputs:

- `configs/research_factory/clean_windows.json`
- `artifacts/research/research_factory/diagnostics/btc_15m_zscore_failure_20260519.md`
- updated observation source audit
- updated candidate verdict board artifact

## Database Schema / Tables / Indexes / Constraints

No database schema changes are in scope. DB access is read-only. The audit
checks:

- `aats_research.governance.observation_results`
- `aats_live_derivatives.public.decision_audit_records`
- `aats_live_derivatives.public.strategy_sleeve_intents`

No indexes or constraints are added.

## Transactions, Consistency, Concurrency

No transactions are opened for writes. Artifact updates are local file writes
only. Generated Research Factory board artifacts remain under
`artifacts/research`.

## Authorization, Authentication, Data Security

Environment files may be loaded only in process for connectivity checks. Secrets
must not be printed. The workflow does not call OKX, does not write paper
orders, and does not write live runtime state.

## Error Handling And Idempotency

If DB credentials cannot connect from the local process, the audit records that
as an operational caveat and uses WSL2 container read-only probes where
available. Verdict board updates are idempotent over workflow summaries.

## State Transition And Lifecycle

No lifecycle expansion is allowed. Candidate lifecycle remains:

```text
workflow_summary -> candidate_verdict_board -> reject | keep_observing | positive_executable_edge
```

## Caching And Performance

No runtime cache is touched. Clean-window config prevents repeated rediscovery
of known-good Gold replay windows.

## Logging, Monitoring, Auditing

The audit documents the observation source status and clearly marks controlled
observation artifacts as not production-derived shadow/paper evidence.

## Testing Strategy

- Unit-test factor DSL nested rolling evaluation.
- Unit-test clean-window config parseability and no-runtime-write flags.
- Re-run focused Research Factory unit tests.

## Migration, Rollback, Compatibility

No migration is required. Rollback is removing the new config/doc/test changes
and reverting the factor evaluator bugfix if needed.

## Configuration And Environment Isolation

`clean_windows.json` is research config only; it is not a runtime config and is
not read by live trading paths. No `.env` content is printed.

## Code Organization And Dependencies

Keep changes inside existing Research Factory modules, docs, tests, and config.
No new dependencies are introduced.

## Documentation And Operations Manual

Update the observation source audit and create a focused BTC 15m ZScore failure
diagnostic artifact. Operator action remains: gather real candidate-bound
shadow/paper events before treating observation results as production-derived
evidence.

## Deployment And Acceptance Criteria

No deployment is in scope.

Acceptance:

- Clean BTC 1h/15m windows are recorded in parseable config.
- BTC 15m ZScore failure is diagnosed.
- Observation source audit states whether real candidate-bound shadow/paper
  evidence exists.
- Verdict board contains the clean-window candidates.
- No runtime write, active parameter mutation, OKX write, or dry-run execution.
