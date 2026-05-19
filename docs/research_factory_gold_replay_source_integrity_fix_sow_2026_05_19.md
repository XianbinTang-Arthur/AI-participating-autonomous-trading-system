# Research Factory Gold Replay Source Integrity Fix SOW

Date: 2026-05-19

## Business Objectives and Boundaries

Fix Gold replay source integrity evidence so Research Factory can distinguish candle dataset lineage from funding dataset lineage. The immediate defect is that `source_funding_dataset_version` is null for Gold replay bars even when funding rates are present.

This work is research-only. It must not change live runtime behavior, production deployment state, active parameters, runtime configuration, OKX access, or dry-run execution.

## Module Responsibilities and Domain Model

- Gold replay builders must populate source lineage fields for every evidence-bearing replay bar.
- Research Factory evidence reports must continue to fail closed when funding lineage is missing.
- Backfill scripts or one-off repair steps may update only RDP research Gold replay tables.

## Input and Output Interfaces

Inputs:
- Existing Gold replay bar tables.
- Existing upstream candle/funding source metadata.
- `RDP_DATABASE_URL` loaded only inside the process from `.env.research` when database repair or validation is needed.

Outputs:
- Populated `source_funding_dataset_version` values for Gold replay rows with funding evidence.
- Updated code/tests preventing future null funding lineage when funding data is present.
- Re-run workflow/verdict evidence for the three candidate sprint factors.

## Database Schema / Tables / Indexes / Constraints

No schema change is intended. The repair targets existing Gold replay tables, especially tables with columns:

- `source_candle_dataset_version`
- `source_funding_dataset_version`
- `build_run_id`
- `aligned_funding_rate`

## Transactions, Consistency, Concurrency

Any data backfill must run in a single transaction per table and report affected row counts without printing credentials. It must be idempotent: rerunning should not create new values or churn existing non-null funding versions.

## Authorization, Authentication, Data Security

Secrets must not be printed. The database URL may be loaded into the child process environment from `.env.research`, but it must not appear in logs, docs, test output, or final reports.

## Error Handling and Idempotency

If upstream funding source metadata cannot be found, the repair must stop and report the missing source rather than inventing ambiguous lineage. If a deterministic research-only backfill source is justified, it must be documented and tested.

## State Transition and Lifecycle

This change does not introduce new governance stages. It repairs evidence source metadata so existing lifecycle stages can produce valid source integrity reports.

## Caching and Performance

No runtime cache changes. Backfill updates should be scoped by table and null funding-version rows only.

## Logging, Monitoring, Auditing

Repair output should include only table names, row counts, and high-level version summaries. It must not include connection strings or secrets.

## Testing Strategy

- Unit tests for Gold replay source lineage propagation or evidence integrity behavior.
- Focused Research Factory tests.
- Full unit suite when code changes land.
- WSL2 narrow integration sanity where relevant.
- Re-run candidate workflow/verdict artifacts to verify current Research Factory behavior.

## Migration, Rollback, Compatibility

No schema migration. Rollback for data repair is to restore `source_funding_dataset_version` to null only for rows updated by this specific backfill, if needed. Code remains backward-compatible with historical rows but continues to fail closed if funding lineage is missing.

## Configuration and Environment Isolation

Use `.env.research` only for the research database connection and only inside process environment. Do not read live runtime env files, do not deploy, and do not write runtime configuration.

## Code Organization and Dependencies

Prefer existing data platform and Research Factory modules. Avoid new dependencies and unrelated refactors.

## Documentation and Operations Manual

Update this SOW and final delivery notes with the exact validation evidence, affected tables, and remaining data-quality caveats.

## Deployment and Acceptance Criteria

Acceptance:

- Gold replay rows with non-null funding evidence no longer have null `source_funding_dataset_version`.
- Rebuilt or backfilled Gold replay tables pass source-funding lineage checks when the rest of dataset quality is valid.
- Three candidate workflows are revalidated or their remaining blockers are explicitly reported.
- All outputs stay under `artifacts/research`.
- No active parameter write, runtime config write, OKX write, deployment, or dry-run execution occurs.

## Repair Notes

Implemented repair:

- `load_silver_funding()` now reads `dataset_version` from Silver funding rows.
- `align_funding_to_bars()` now carries the aligned funding row's
  `dataset_version` alongside funding rate and source timestamp.
- `build_gold_replay_bars()` writes `source_funding_dataset_version` from the
  aligned funding row, with a fallback to the single observed funding dataset
  version for the build window.

Research DB backfill:

```text
gold.market_swap_replay_bars_15m BTC-USDT-SWAP: 6057 rows -> v1.0
gold.market_swap_replay_bars_15m ETH-USDT-SWAP: 6057 rows -> v1.0
gold.market_swap_replay_bars_1h BTC-USDT-SWAP: 8133 rows -> v1.0
gold.market_swap_replay_bars_1h ETH-USDT-SWAP: 3765 rows -> v1.0
```

Post-repair source integrity:

```text
BTC 1h momentum: source_integrity.passed=true, funding version=v1.0
BTC 15m ZScore reversal: source_integrity.passed=true, funding version=v1.0
BTC 1h funding drift: source_integrity.passed=true, funding version=v1.0
```

Remaining blockers are dataset-quality issues, not source funding lineage:

```text
1h: bar gaps, max gap exceeded, funding_missing_ratio > 0
15m: valid/test segment rows below threshold, bar gaps, max gap exceeded
```
