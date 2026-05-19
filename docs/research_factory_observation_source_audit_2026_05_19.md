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

The same read-only probe found only these shadow/paper/intent/observation
candidate tables:

```text
bronze.market_orderbook_bbo
bronze.market_orderbook_books5
bronze.market_orderbook_payloads
governance.observation_results
silver.market_orderbook_metrics_15m
```

No candidate-bound shadow decision or paper intent table was found in the RDP
database search path.

Gold replay funding source lineage was repaired after the initial audit. It no
longer blocks current real-factor workflows before observation:

```text
gold.market_swap_replay_bars_1h:
  BTC-USDT-SWAP rows: 8133
  source_funding_dataset_version non-null rows: 8133
  source_funding_dataset_versions: v1.0

  ETH-USDT-SWAP rows: 3765
  source_funding_dataset_version non-null rows: 3765
  source_funding_dataset_versions: v1.0

gold.market_swap_replay_bars_15m:
  BTC-USDT-SWAP rows: 6057
  source_funding_dataset_version non-null rows: 6057
  source_funding_dataset_versions: v1.0

  ETH-USDT-SWAP rows: 6057
  source_funding_dataset_version non-null rows: 6057
  source_funding_dataset_versions: v1.0
```

Post-repair revalidation shows `SourceIntegrityReport.passed=true` and
`source_funding_dataset_versions=('v1.0',)` for the tested BTC 1h and 15m
candidate evidence bundles. The remaining blockers are dataset-quality issues:
bar gaps, 1h funding missing ratio, and insufficient 15m valid/test segment
rows.

Follow-up historical backfill reduced the usable-window blocker:

```text
BTC-USDT-SWAP 15m candle backfill:
  scripts/rdp_deep_backfill_api.py
  pages: 91
  staged/bronze/silver rows: 9100
  rebuilt Gold rows: 15195

BTC-USDT-SWAP / ETH-USDT-SWAP funding backfill:
  scripts/rdp_deep_backfill_funding.py
  target: 2025-12-13
  rows added: 0
  reason: OKX funding-rate-history returned no rows before 2026-01-15
```

The resulting clean funding-present windows are:

```text
BTC-USDT-SWAP 15m:
  2026-01-15 00:00 Asia/Shanghai -> 2026-04-17 15:30 Asia/Shanghai
  rows: 8895

BTC-USDT-SWAP 1H:
  2026-01-15 00:00 Asia/Shanghai -> 2026-04-17 14:00 Asia/Shanghai
  rows: 2223
```

Research Factory revalidation on the clean window produced:

```text
wf_btc_1h_momentum_clean_20260519:
  verdict: reject
  reason: candidate gate failed

wf_btc_15m_zscore_clean_20260519:
  verdict: reject
  reason: candidate gate failed

wf_btc_1h_funding_drift_clean_obsfix_20260519:
  verdict: keep_observing
  reason: observation sample is still insufficient
```

The `keep_observing` result still uses a controlled observation artifact. The
audit conclusion is unchanged: candidate-bound shadow/paper events must be
connected before treating observation results as true production-derived
evidence.

## Recommended Next Step

Next repair the remaining Gold replay dataset-quality gaps. Then use the
read-only JSONL exporter and generator for controlled artifact inputs. After the
team identifies the authoritative shadow/paper stores, add a separate read-only
extraction job that writes source JSONL artifacts for this exporter. That
extractor should:

```text
read shadow/paper evidence
  -> write source JSONL under artifacts/research
  -> normalize to canonical event JSONL under artifacts/research
  -> aggregate summary metrics
  -> write observation summary JSON under artifacts/research
```

It must not write runtime config, active parameters, OKX orders, or paper orders.

## Follow-up Probe: Candidate-bound Observation Source

Follow-up date: 2026-05-19

A second read-only probe was run against the local WSL2 Postgres container.
Windows-local SQLAlchemy connectivity is not treated as a requirement here:
the project data store lives in WSL2. No connection strings were printed. The
WSL2 probe used `docker exec aats-postgres psql` for read-only schema/count
checks.

`aats_research` contains governance observation rows:

```text
governance.observation_results rows: 21
status distribution:
  completed: 21
```

Those rows remain release/family/timeframe/checklist records. Their payload keys
are:

```text
checklist
combo_key
evaluated_at
family
observation_window_hours
recommendation
regression_count
release_id
started_at
status
timeframe
warning_count
window_active
```

They still do not contain Research Factory identity:

```text
recommendation_id
candidate_id
experiment_id
```

and they do not contain canonical observation metrics:

```text
fillable_ratio
partial_fill_ratio
fee_bps
slippage_bps
funding_bps
cost_adjusted_edge_bps
drawdown
metric_drift
```

`aats_live_derivatives` does contain live decision/intention surfaces:

```text
public.strategy_sleeve_intents rows: 509913
public.decision_audit_records rows: 1143948
public.decision_audit_records with non-empty ai_shadow_decision_refs: 2388
```

These are useful runtime audit sources, but they are not yet candidate-bound
Research Factory observation sources. The recent rows are strategy/family
runtime decisions such as `advisory_only` or `hold_current`; they do not carry
the current Research Factory `recommendation_id`, `candidate_id`, and
`experiment_id` tuple.

Current conclusion:

```text
Funding drift still lacks true candidate-bound shadow/paper observation events.
The current keep_observing verdict must remain observation-limited until a
read-only extractor can bind runtime shadow/paper evidence to the Research
Factory candidate identity.
```
