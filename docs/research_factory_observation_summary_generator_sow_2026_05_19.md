# Research Factory Observation Summary Generator SOW

Date: 2026-05-19

## Business objectives and boundaries

Add a read-only observation summary generator so Research Factory governance
workflows can consume reproducible shadow/paper observation evidence instead of
handwritten summary JSON.

This does not add a new governance package, review, approval, safety policy,
execution layer, dry-run executor, apply bridge, runtime write path, active
parameter mutation, or OKX write path.

## Module responsibilities and domain model

New module:

- `ObservationEventRecord`
- `ObservationSummary`
- `build_observation_summary_from_events()`
- `write_observation_summary()`

New CLI:

- `scripts/rdp_generate_research_observation_summary.py`

The generator reads event JSONL artifacts and writes an observation summary JSON
that matches `research_observation_summary_v1`.

Upstream raw shadow/paper export rows should first pass through
`observation_event_exporter.py` so missing execution evidence fails before the
summary step.

## Input/output interfaces

Inputs:

- event JSONL under `artifacts/research`
- expected `recommendation_id`
- expected `candidate_id`
- expected `experiment_id`
- expected mode: `shadow` or `paper`

Outputs:

- observation summary JSON under `artifacts/research`

Each input event can include:

```text
ts
bar_ts
recommendation_id
candidate_id
experiment_id
mode
signal
paper_intent
fillable
partial_fill
fee_bps
slippage_bps
funding_bps
cost_adjusted_edge_bps
drawdown
metric_drift
abort_triggered
abort_reason
```

## Database schema / tables / indexes / constraints

No database changes.

## Transactions, consistency, concurrency

The output summary is written atomically. Existing output files are not
overwritten unless the CLI is run with `--overwrite`.

## Authorization, authentication, data security

The generator reads no credentials, does not read `.env`, and only reads/writes
under `artifacts/research`.

## Error handling and idempotency

Failures are explicit: empty events, identity mismatches, mode mismatches,
missing timestamps, non-finite metrics, unsafe paths, and duplicate output paths
fail closed.

## State transition and lifecycle

```text
shadow/paper event JSONL
  -> observation summary JSON
  -> existing ReadOnlyObservationDataSource
  -> ObservationResult / ObservationGateResult
```

The generator produces evidence input only. It does not run observations or
trading.

## Caching and performance

No caching. The generator streams small JSONL files line by line.

## Logging, monitoring, auditing

The output summary contains source event count, source artifact ref, observation
window, and generated timestamp metadata.

## Testing strategy

Add tests for:

- shadow event aggregation
- paper event aggregation
- identity mismatch rejection
- output path must be under `artifacts/research`
- no overwrite without explicit opt-in
- CLI JSON success and failure

## Migration, rollback, compatibility

No migrations. The output summary uses the existing
`research_observation_summary_v1` schema.

## Configuration and environment isolation

No env vars. All inputs are explicit CLI args.

## Code organization and dependencies

No new third-party dependencies.

## Deployment and acceptance criteria

Acceptance requires lint, focused generator tests, Research Factory unit tests,
full unit tests, and WSL2 RDP workflow sanity. No deployment is required.
