# Research Factory Candidate Verdict Sprint Readiness

Date: 2026-05-19

## Scope

This readiness note checks whether the current Research Factory operational
chain can run the requested three real BTC candidates through governance
workflow and verdict board.

It does not authorize live trading, runtime mutation, active parameter updates,
runtime config writes, OKX writes, dry-run execution, paper order writes, or
production deployment.

## Candidate Checks

The requested factor DSL expressions parse successfully:

```text
BTC 1h momentum:
  Return(close, 1)

BTC 15m short momentum / reversal:
  ZScore(Return(close, 4), 20)

BTC 1h funding drift:
  Mean(funding_rate, 8) - Mean(funding_rate, 32)

Fallback volume factor:
  Delta(volume, 4) / Mean(volume, 20)
```

Gold replay availability was checked read-only from WSL2 using
`RDP_DATABASE_URL` loaded into process environment from `.env.research`; the
connection string was not printed.

```text
candidate_id: btc_1h_momentum
table: gold.market_swap_replay_bars_1h
window: 2026-01-01 to 2026-05-15
rows: 3167
funding_rows: 2839

candidate_id: btc_15m_zscore_reversal
table: gold.market_swap_replay_bars_15m
window: 2026-03-20 to 2026-05-15
rows: 3109
funding_rows: 3109

candidate_id: btc_1h_funding_drift
table: gold.market_swap_replay_bars_1h
window: 2026-01-01 to 2026-05-15
rows: 3167
funding_rows: 2839
```

These row counts satisfy the real factor research profile's dataset volume
expectations. The current blocker is not Gold replay coverage or factor DSL
support.

## Observation Evidence Check

The current artifact tree does not contain Research Factory observation event
JSONL or observation summary JSON artifacts for these candidates.

The RDP database contains `governance.observation_results`, but sampled payloads
only expose release/family/timeframe/checklist style review metadata:

```text
payload keys:
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

That table does not provide the current Research Factory observation summary
contract fields:

```text
recommendation_id
candidate_id
experiment_id
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
```

Because it cannot be bound to the current candidate/recommendation identity and
lacks execution-observation metrics, it must not be reused as if it were a
valid `research_observation_summary_v1`.

## Execution Evidence Check

The current artifact tree contains an older smoke execution summary fixture, but
it does not include the full Research Factory execution evidence identity
contract. A real workflow run still needs contract-compatible execution evidence
for each candidate window.

Required fields include:

```text
schema_version
source_run_id
symbol
timeframe
window_start
window_end
dataset_fingerprint
```

or explicit dataset fingerprint compatibility with a reason where the selected
research profile allows weak compatibility evidence.

## Current Verdict Sprint Status

```text
btc_1h_momentum:
  status: blocked_missing_observation_evidence
  next_action: export real shadow/paper observation events to artifacts/research

btc_15m_zscore_reversal:
  status: blocked_missing_observation_evidence
  next_action: export real shadow/paper observation events to artifacts/research

btc_1h_funding_drift:
  status: blocked_missing_observation_evidence
  next_action: export real shadow/paper observation events to artifacts/research
```

No candidate verdict was generated because producing `reject`,
`keep_observing`, or `positive_executable_edge` without candidate-bound
observation evidence would make the verdict board misleading.

## Operational Next Step

Use the existing read-only observation summary generator only after a real
shadow/paper event JSONL exists under `artifacts/research`.

Expected flow:

```text
real shadow/paper events
  -> artifacts/research/.../observation_events.jsonl
  -> scripts/rdp_generate_research_observation_summary.py
  -> research_observation_summary_v1
  -> scripts/rdp_run_research_governance_workflow.py
  -> workflow_summary.json
  -> scripts/rdp_update_candidate_verdict_board.py
```

The exporter that produces `observation_events.jsonl` must be read-only and must
not write runtime config, active parameters, OKX orders, paper orders, or any
runtime state.
