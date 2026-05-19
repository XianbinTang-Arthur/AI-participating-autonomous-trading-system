# Research Factory Observation Source Audit

Date: 2026-05-19

## Scope

This audit checks the current observation summary input used by the Research
Factory governance workflow. It does not change runtime behavior and does not
authorize live trading, runtime mutation, active parameter updates, OKX writes,
paper order writes, or production deployment.

## Current Observation Summary Source

Current code path:

```text
ReadOnlyObservationDataSource
  -> ShadowObservationDataSource / PaperObservationDataSource
  -> observation summary JSON under artifacts/research
  -> ObservationResult
```

The source adapter reads an existing JSON artifact using
`require_research_artifact_json_file()` and requires the file to stay under
`artifacts/research`.

## Identity And Window Binding

The observation summary must include and match:

```text
recommendation_id
candidate_id
experiment_id
mode
observation_start
observation_end
```

`recommendation_id`, `candidate_id`, and `experiment_id` are checked against the
current `ResearchRecommendation`. `mode` must match the selected shadow or paper
source.

## Evidence Fields

The current summary contract requires:

```text
observed_bars
observed_events
signal_count
paper_intent_count
fillable_ratio
partial_fill_ratio
fee_bps_mean
slippage_bps_mean
funding_bps_mean
cost_adjusted_edge_bps_mean
drawdown
metric_drift
abort_triggered
abort_reason
```

This is enough to build `ObservationResult` and run `ObservationGateResult`.

## Shadow/Paper Provenance Status

Current status: **summary-artifact adapter plus read-only event JSONL exporter and generator**.

The adapter verifies the summary artifact shape and identity, but the code does
not yet prove that the summary was automatically generated from a live shadow
signal store, paper intent store, fillability estimator, or portfolio shadow
metrics. In other words, the summary can represent trustworthy shadow/paper
evidence if produced by a controlled upstream process, but Research Factory does
not currently own a direct shadow/paper database exporter.

The `observation_event_exporter.py` path can normalize already-exported
shadow/paper event JSONL artifacts into canonical Research Factory observation
events, and `observation_summary_generator.py` can aggregate those canonical
events into `research_observation_summary_v1`. This remains read-only and
artifact-based: it does not read runtime tables directly, write runtime config,
create paper orders, call OKX, execute dry-runs, or mutate active parameters.

## Reproducibility

Reproducibility is partial:

- The summary file is read from `artifacts/research`.
- Dataset/window identity is present in the observation window fields.
- Candidate/recommendation/experiment identity is required.
- Generated summaries include source event count, source artifact ref, and
  generation timestamp when produced through the JSONL generator.

Missing:

- source run id
- source system id
- source query/window extraction parameters for direct shadow/paper database
  exports
- direct automatic link from raw shadow signal or paper intent stores into the
  source JSONL artifact

## Current Gaps

1. No direct shadow/paper database exporter exists in this Research Factory
   path; the new exporter requires a controlled JSONL source artifact.
2. The summary does not yet record source run id or source system id.
3. The summary does not prove whether paper intents came from a paper-only
   system or a manually assembled artifact.
4. Fillability/slippage/funding metrics are required numerically by the
   canonical event path, but their upstream calculation provenance is not yet
   encoded beyond source event ids.

## RDP Database Probe

A read-only WSL2 probe of `RDP_DATABASE_URL` found `governance.observation_results`.
The sampled payloads are release/family/timeframe/checklist review records, not
candidate-bound Research Factory observation events. They do not include
`recommendation_id`, `candidate_id`, `experiment_id`, fillability, partial fill,
fee, slippage, funding, cost-adjusted edge, drawdown, or metric drift fields.

Therefore those rows should not be converted into
`research_observation_summary_v1` for new Research Factory candidates without a
separate, explicit mapping and metric extraction layer.

## Recommended Next Step

Use the read-only JSONL exporter and generator for controlled artifact inputs.
After the team identifies the authoritative shadow/paper stores, add a separate
read-only extraction job that writes source JSONL artifacts for this exporter.
That extractor should:

```text
read shadow/paper evidence
  -> write source JSONL under artifacts/research
  -> normalize to canonical event JSONL under artifacts/research
  -> aggregate summary metrics
  -> write observation summary JSON under artifacts/research
```

It must not write runtime config, active parameters, OKX orders, or paper orders.
