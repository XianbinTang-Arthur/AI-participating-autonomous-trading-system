# Task255 Reconciliation Contraction Active Root Cause Audit

## 结论

`reconciliation_contraction_active` 来自最新 scoped reconciliation report 的 `SOFT_MISMATCH`，不是 stale recovery state。

最新 recovery snapshot 显示 `safe_to_trade=true`、`resume_eligible=true`、`halt_required=false`、`review_required=false`、`only_reduce_required=false`。因此当前不是硬停机、不是 only-reduce、不是 resume-blocking；它是 `SleeveBudgetController` 对所有非 `CLEAN` reconciliation severity 应用的软预算收缩。

## 证据

审计时间：`2026-04-26T10:10:50Z` 至 `2026-04-26T10:13Z`

最新 runtime truth：

- `blocking_findings=[]`
- `deployed_matches_windows=true`
- Windows/main 与 origin/main：ahead `0`、behind `0`
- 最新 decision：`decision_425d381033644e7b8492fe78703cc2a4`
- 最新 blocker：`reconciliation_contraction_active`
- 最新 decision execution chain：execution plan/order/order_state/fill 计数全为 `0`

最新 reconciliation report：

- `reconciliation_id=recon_18529d1ab70c476695497340b711b1c9`
- `as_of_ts=2026-04-26 18:12:25.570584+08:00`
- `severity=SOFT_MISMATCH`
- `halt_required=false`
- `only_reduce_required=false`
- `review_required=false`
- `resume_blocking=false`
- `recommended_operator_action=investigate_state_divergence`
- `safety_impacts=["fill_history_visibility_is_incomplete"]`
- `mismatch_reasons=["local_exchange_fill_set_diverges_from_exchange_fill_set"]`

最新 reconciliation findings：

- 25 条 `historic_orphan_fill`
- reason_code 全部为 `local_fill_older_than_exchange_lookback_window`
- severity_class 全部为 `info`
- `blocks_resume=false`
- `halt_required=false`
- `only_reduce_required=false`

最新 recovery snapshot：

- `snapshot_id=reconstate_df17d962e9b84f6e91bc4ea05eff9f2d`
- `reconciliation_id=recon_33be39aa7be9432188697954ff06cef2`
- `created_at=2026-04-26 18:00:00.136032+08:00`
- `recovery_state=degraded_continue`
- `safe_to_trade=true`
- `resume_eligible=true`
- `halt_required=false`
- `review_required=false`
- `only_reduce_required=false`
- `resume_blocked_reasons_json=[]`

Order/fill state：

- latest fill：`2026-04-17 17:51:39.871040+08:00`
- latest order：`2026-04-17 17:51:57.848109+08:00`，state `CANCELED`
- current non-terminal `order_states` count：`0`
- `order_states` status counts：`FILLED=25`、`CANCELED=3`
- since latest reconciliation report：orders `0`、fills `0`

最近 12 小时 reconciliation cadence：

- scoped reconciliation reports 持续生成，且每小时均为 `SOFT_MISMATCH`
- 这说明状态新鲜，不是任务停止或 stale snapshot。

## 代码路径

`aats/services/strategy_engines/sleeve_budget_controller.py` 在以下条件下加入 `reconciliation_contraction_active`：

- `latest_reconciliation.only_reduce_required`
- `latest_reconciliation.review_required`
- 或 `latest_reconciliation.severity` 不在 `{"", "CLEAN"}`

当前 live report 满足第三个条件：`severity=SOFT_MISMATCH`。

## 对 no-trade 的影响

本轮审计显示 `reconciliation_contraction_active` 是有效的软预算收缩信号，但不是唯一 no-trade 因素。

最新 allocation：

- `portfolio_requested_notional=0`
- `portfolio_approved_notional=0`
- `execution_legs_count=0`
- `route_action=advisory_only`
- `primary_family=independent`

Sleeve 级别：

- `directional` 有 `approved_for_execution=true`、`route_action=override_target`、`effective_scale=0.5`，但当前 live carrier 是 `independent`，不能据此认定 directional 已可实盘执行。
- `independent` 的 `route_action=advisory_only`、`approved_for_execution=false`，原因包含：
  - `independent_long_book_signal_above_entry_threshold`
  - `independent_short_book_signal_below_entry_threshold`
  - `independent_family_candidate_inactive`
  - `candidate_execution_incompatible`
  - `reconciliation_contraction_active`
  - `composed_as_advisory_only`

因此，当前更准确的 no-trade 描述是：

> Reconciliation SOFT_MISMATCH 正在触发软预算收缩；同时 live carrier independent 当前仍是 inactive / execution incompatible / advisory-only，所以最新 decision 没有 execution plan、order 或 fill。

## Verdict

`reconciliation_contraction_active` 是新鲜、预期的软收缩状态，不是 stale recovery state。

当前 no-trade 不应通过放宽 risk/execution gate 或手动下单解决。下一步应修正 truth attribution 的表达：不要把 `reconciliation_contraction_active` 单独当作最终 blocker，而要展示多因素链路：`SOFT_MISMATCH contraction + independent inactive/execution incompatible + advisory_only + zero approved notional`。

## 验收

- 已追踪到具体 source：`reconciliation_reports.recon_18529d1ab70c476695497340b711b1c9`
- 已确认 state freshness：最近 12 小时持续生成 scoped `SOFT_MISMATCH` reports
- 已确认不是 hard halt / only-reduce / resume-blocking
- 已确认 no live order behavior changed

## 回滚

本文档为只读审计产物，无运行时回滚需求。
