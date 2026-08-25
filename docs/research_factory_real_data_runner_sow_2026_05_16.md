# Research Factory Real Data Runner SOW - 2026-05-16

> 历史任务材料：本文描述 v1 runner；其中 test 评价路径已被 Phase 3V 替代。当前 train/valid
> 选择、test 内容封存及 execution evidence 窗口契约见
> [`task/fs_004_research_selection_holdout_sow_2026_08_25.md`](task/fs_004_research_selection_holdout_sow_2026_08_25.md)。

## Business Objectives and Boundaries

Promote Research Factory from synthetic smoke validation to a real-data research loop that can evaluate safe factor DSL candidates against AATS Gold replay bars and execution realism evidence. The goal is to start answering whether a candidate has cost-adjusted executable edge on real AATS data.

This work remains research-only. It does not submit orders, call OKX, mutate active parameters, change runtime config, deploy services, run generated Python, or connect to the live trading runtime.

## Module Responsibilities and Domain Model

New responsibilities:

- `GoldReplayDataSource`: read-only adapter from RDP Gold replay bar tables into Research Factory `GoldBarRecord` rows.
- `ResearchFactoryExperimentConfig`: typed CLI/module config for real-data experiments.
- `run_research_factory_experiment()`: real-data equivalent of the smoke loop.
- `require_execution_realism`: candidate recommendation gate that requires execution cost summary metrics before `ready_for_review`.

Existing responsibilities remain:

- `GoldBarDatasetHandler`: segment and validate bars.
- Factor DSL evaluator: deterministic feature calculation.
- Baseline benchmark: factor-only long/flat metrics.
- Execution realism adapter: map `execution_cost_summary.json` to metrics.
- Candidate/recommendation/registry/recorder: research-only artifact lifecycle.

## Input/Output Interfaces

CLI:

```text
scripts/rdp_run_research_factory_experiment.py
  --symbol BTC-USDT-SWAP
  --timeframe 15m
  --start 2026-05-01T00:00:00Z
  --end 2026-05-08T00:00:00Z
  --factor-expression "Return(close, 1)"
  --label-horizon-bars 1
  --execution-cost-summary artifacts/.../execution_cost_summary.json
```

The script reads the database URL only from an environment variable, default `RDP_DATABASE_URL`. It does not read `.env` files.

Outputs:

```text
artifacts/research/research_factory/experiments/<experiment_id>/
  experiment_spec.json
  metrics_snapshot.json
  candidate_artifact.json
  research_recommendation.json
  experiment_manifest.json

artifacts/research/research_factory/registry/research_memory.jsonl
```

## Database Schema / Tables / Indexes / Constraints

No schema changes. The runner reads Gold replay tables resolved through existing whitelist table-name helpers:

- `gold.market_swap_replay_bars_1m`
- `gold.market_swap_replay_bars_5m`
- `gold.market_swap_replay_bars_15m`
- `gold.market_swap_replay_bars_1h`

Optional `source_candle_dataset_version` filtering is supported.

## Transactions, Consistency, Concurrency

Database access is read-only from the runner perspective. Artifact writes use existing atomic recorder/registry writers. Concurrent writes to the same experiment id or registry path remain unsupported in V1.

## Authorization, Authentication, Data Security

No `.env` files are read. The CLI reads only the configured database URL environment variable and never prints it. No credentials are written to artifacts. Failure reasons continue to be redacted by recorder/registry layers.

## Error Handling and Idempotency

Invalid symbol/timeframe/window/expression/paths raise before candidate generation. Missing Gold bars or empty segments fail the experiment and write a failure artifact when the recorder has started. With `--overwrite`, only the target experiment directory under the research artifact root is removed.

Execution realism is required by default for real-data experiments. Missing or incomplete execution cost summary prevents `ready_for_review`.

## State Transition and Lifecycle

Real-data lifecycle:

`Gold replay bars -> DatasetSpec -> factor evaluation -> baseline metrics -> execution realism merge -> gate -> CandidateArtifact -> ResearchRecommendation -> ResearchMemoryRegistry -> manifest`

If execution realism is missing or the candidate gate fails, the experiment is marked failed and the registry records `failed` or `gate_failed`.

## Caching and Performance

V1 loads the selected Gold window into memory. This is acceptable for bounded research windows. Large-scale studies should add pagination or a database-backed feature store in a later phase.

## Logging, Monitoring, Auditing

The runner returns stable JSON with experiment id, artifact dir, status, candidate/ref outputs, and registry ref. Artifacts and registry entries provide auditability.

## Testing Strategy

Add unit tests covering:

- Gold replay rows convert into `GoldBarRecord` with source watermark.
- Real runner succeeds with injected data source and execution summary.
- Real runner fails when execution realism is required but missing.
- Recommendation builder rejects `ready_for_review` when `require_execution_realism=True` and metrics are incomplete.
- CLI fails safely when the database URL environment variable is missing.

## Migration, Rollback, Compatibility

This is additive. Smoke runner behavior remains unchanged. Rollback removes the real-data runner module, CLI, recommendation flag, SOW, and tests.

## Configuration and Environment Isolation

No new persistent config files. CLI defaults:

- artifact root: `artifacts/research/research_factory/experiments`
- registry path: sibling `artifacts/research/research_factory/registry/research_memory.jsonl`
- DB URL env var: `RDP_DATABASE_URL`
- execution realism required: true

## Code Organization and Dependencies

No new third-party dependencies. SQLAlchemy is already part of AATS/RDP. The new module lives under `aats/data_platform/research_factory/`.

## Documentation and Operations Manual

Operators should run the real-data runner only against bounded windows and should provide an execution cost summary when expecting a recommendation-ready candidate. Without execution realism, the run should remain failed or draft-level evidence only.

## Deployment and Acceptance Criteria

Acceptance requires:

- Focused unit tests pass.
- `ruff check aats/ --fix` passes.
- Full unit suite passes.
- WSL2 real-data CLI no-DB-url safety path passes.
- Existing WSL2 data-platform integration smoke remains green.
