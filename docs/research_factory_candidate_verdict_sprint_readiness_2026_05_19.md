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

## Candidate Verdict Sprint Run

The three requested candidates were run through
`scripts/rdp_run_research_governance_workflow.py` on 2026-05-19 using
`real_factor_research`. `RDP_DATABASE_URL` was loaded into the process
environment from `.env.research`; the connection string was not printed.

Because no candidate-bound real shadow/paper observation events exist yet, the
temporary observation input artifacts used for this sprint are controlled
research artifacts only. They are not claimed as true shadow/paper evidence.
The workflows did not reach observation review because the Gold replay evidence
gate rejected all three candidates first.

Outputs:

```text
workflow summaries:
  artifacts/research/research_factory/workflows/wf_btc_1h_momentum_20260519/workflow_summary.json
  artifacts/research/research_factory/workflows/wf_btc_15m_zscore_20260519/workflow_summary.json
  artifacts/research/research_factory/workflows/wf_btc_1h_funding_drift_20260519/workflow_summary.json

verdict board:
  artifacts/research/research_factory/verdicts/candidate_verdict_board.jsonl
  artifacts/research/research_factory/verdicts/candidate_verdict_board.md
```

Verdicts:

```text
BTC 1h momentum:
  verdict: reject
  reason: evidence bundle failed
  blocking failures:
    dataset_quality bar gap ratio > 0
    dataset_quality max gap exceeded expected 1h interval
    funding missing ratio > 0
    source funding dataset version missing/untraceable

BTC 15m ZScore reversal:
  verdict: reject
  reason: evidence bundle failed
  blocking failures:
    valid/test segment rows below real_factor_research thresholds
    dataset_quality bar gap ratio > 0
    dataset_quality max gap exceeded expected 15m interval
    source funding dataset version missing/untraceable

BTC 1h funding drift:
  verdict: reject
  reason: evidence bundle failed
  blocking failures:
    dataset_quality bar gap ratio > 0
    dataset_quality max gap exceeded expected 1h interval
    funding missing ratio > 0
    source funding dataset version missing/untraceable
```

## Source Integrity Revalidation

Gold replay source lineage was repaired on 2026-05-19. The repair populated
`source_funding_dataset_version` from the unique upstream
`silver.market_swap_funding.dataset_version` for the affected Gold swap replay
tables.

Post-repair distribution:

```text
gold.market_swap_replay_bars_15m:
  BTC-USDT-SWAP rows: 6057
  source_funding_dataset_version non-null rows: 6057
  source_funding_dataset_versions: v1.0

  ETH-USDT-SWAP rows: 6057
  source_funding_dataset_version non-null rows: 6057
  source_funding_dataset_versions: v1.0

gold.market_swap_replay_bars_1h:
  BTC-USDT-SWAP rows: 8133
  source_funding_dataset_version non-null rows: 8133
  source_funding_dataset_versions: v1.0

  ETH-USDT-SWAP rows: 3765
  source_funding_dataset_version non-null rows: 3765
  source_funding_dataset_versions: v1.0
```

The three candidate workflows were re-run after the repair. Source integrity now
passes for the evidence bundles:

```text
BTC 1h momentum:
  source_integrity.passed: true
  source_candle_dataset_versions: v1.0
  source_funding_dataset_versions: v1.0
  remaining blocker:
    dataset_quality bar gap ratio > 0
    dataset_quality max gap exceeded expected 1h interval
    funding missing ratio > 0

BTC 15m ZScore reversal:
  source_integrity.passed: true
  source_candle_dataset_versions: v1.0
  source_funding_dataset_versions: v1.0
  remaining blocker:
    valid/test segment rows below real_factor_research thresholds
    dataset_quality bar gap ratio > 0
    dataset_quality max gap exceeded expected 15m interval

BTC 1h funding drift:
  source_integrity.passed: true
  source_candle_dataset_versions: v1.0
  source_funding_dataset_versions: v1.0
  remaining blocker:
    dataset_quality bar gap ratio > 0
    dataset_quality max gap exceeded expected 1h interval
    funding missing ratio > 0
```

## Historical Backfill And Clean-Window Verdicts

The existing RDP scripts were used to repair the usable research window:

```text
scripts/rdp_detect_gaps.py:
  BTC-USDT-SWAP 1H: no Silver candle gaps detected
  BTC-USDT-SWAP 15m: no Silver candle gaps detected

scripts/rdp_deep_backfill_api.py:
  BTC-USDT-SWAP 15m:
    pulled 91 OKX history-candles pages
    staged/bronze/silver rows: 9100
    rebuilt Gold 15m rows: 15195
  BTC-USDT-SWAP 1H:
    existing Silver start predates target, no API backfill needed

scripts/rdp_deep_backfill_funding.py:
  BTC-USDT-SWAP / ETH-USDT-SWAP:
    target 2025-12-13
    OKX funding-rate-history returned no rows before 2026-01-15
    silver rows added: 0

scripts/rdp_build_gold_all.py:
  BTC-USDT-SWAP 1H Gold rebuilt rows: 8133
  BTC-USDT-SWAP 15m Gold rebuilt rows: 15195
```

Post-backfill clean funding windows:

```text
BTC-USDT-SWAP 15m:
  clean funding-present closed run:
    2026-01-15 00:00 Asia/Shanghai
    -> 2026-04-17 15:30 Asia/Shanghai
    rows: 8895

BTC-USDT-SWAP 1H:
  clean funding-present closed run:
    2026-01-15 00:00 Asia/Shanghai
    -> 2026-04-17 14:00 Asia/Shanghai
    rows: 2223
```

Clean-window workflow/verdict results:

```text
BTC 1h momentum:
  workflow: wf_btc_1h_momentum_clean_20260519
  verdict: reject
  reason: candidate gate failed
  detail: net_annualized_return=-2.472630 <= 0

BTC 15m ZScore reversal:
  workflow: wf_btc_15m_zscore_clean_20260519
  verdict: reject
  reason: candidate gate failed
  detail: candidate_generated=false; net_annualized_return/max_drawdown missing

BTC 1h funding drift:
  workflow: wf_btc_1h_funding_drift_clean_obsfix_20260519
  verdict: keep_observing
  reason: observation sample is still insufficient
  next_action: request_more_observation
```

The funding drift candidate is the first clean-window candidate to pass the
research evidence and candidate gate, then stop correctly at observation because
the controlled observation artifact has only two events. This is a governance
success condition, not permission to apply or execute a dry-run.

Acceptance status:

```text
3 candidate verdicts generated: yes
at least 1 reject: yes
at least 1 keep_observing or positive_executable_edge: yes
```

The clean-window run produced one `keep_observing` candidate and two rejects.
The `keep_observing` result still uses controlled observation artifacts, not a
verified live shadow/paper source pipeline.

## Clean Window Registry

The current known-clean BTC Gold replay windows were recorded in:

```text
configs/research_factory/clean_windows.json
```

The config is research-only. It is not a runtime config and does not authorize
active parameter writes, runtime config writes, OKX writes, dry-run execution,
or production deployment.

Current entries:

```text
BTC-USDT-SWAP 1h:
  2026-01-15 00:00 Asia/Shanghai
  -> 2026-04-17 14:00 Asia/Shanghai
  rows: 2223
  source versions: candle=v1.0, funding=v1.0

BTC-USDT-SWAP 15m:
  2026-01-15 00:00 Asia/Shanghai
  -> 2026-04-17 15:30 Asia/Shanghai
  rows: 8895
  source versions: candle=v1.0, funding=v1.0
```

## BTC 15m ZScore Triage Update

The BTC 15m ZScore clean-window failure was triaged in:

```text
artifacts/research/research_factory/diagnostics/btc_15m_zscore_failure_20260519.md
```

The initial diagnosis was:

```text
B. benchmark/input bug -> fix and rerun
```

Root cause: the parser accepted `ZScore(Return(close, 4), 20)`, but the factor
evaluator treated rolling functions as field-only functions. That made the
factor all-null and caused `candidate_generated=false`. The evaluator now
supports safe nested expressions as rolling inputs, and the focused unit test
confirms this expression produces non-null values after warmup.

The workflow was rerun from WSL2 using the current `/mnt/d/...` worktree and the
RDP database URL obtained from the running `aats-rdp-daemon` container. The URL
was used only as a child-process environment variable and was not printed.

Rerun result:

```text
workflow: wf_btc_15m_zscore_clean_evalfix4_20260519
verdict: reject
reason: candidate gate failed
detail:
  net_annualized_return=-4.064943 <= 0
  max_drawdown=0.217968 > 0.2
```

Updated conclusion:

```text
A. candidate invalid -> keep reject
```

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
