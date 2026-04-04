# Task130: Strategy Families Batch D 交付说明

## 范围

本批次只完成 `independent family` 的业务评估归属迁移，不提前切 allocator / apply / execution 主路径。

已完成：

- 将 `independent` 从 `target_position.py` 内嵌评估抽到独立 family helper
- 在 coordinator / snapshot / audit 中产出真实 `family="independent"` 候选
- 为 `independent` 补上：
  - `expected net edge gating`
  - `close-threshold hysteresis`
  - `execution cost gating`
- 保留当前主执行归属：
  - coordinator 仍选旧主线
  - allocator / apply 主路径仍由 legacy `directional` 承接

## 本批次新增配置

- `strategy_hedge_independent_long_close_threshold`
- `strategy_hedge_independent_short_close_threshold`
- `strategy_hedge_independent_min_safe_net_edge_bps`
- `strategy_hedge_independent_expected_slippage_buffer_bps`
- `strategy_hedge_independent_expected_execution_buffer_bps`
- `strategy_hedge_independent_weak_edge_execution_mode`
- `strategy_hedge_independent_max_acceptable_cost_bps`
- `strategy_hedge_independent_passive_first_enabled`

说明：

- 如果未显式配置 `close_threshold`，会自动继承对应 `entry_threshold`
- 本批次会暴露 `weak_edge_execution_mode / passive_first_enabled`，但不会提前切换真实 executor 行为

## 行为变化

### 1. 真实 family 候选

启用 `strategy_family_independent_enabled=true` 后，snapshot / runtime / audit 不再只显示 placeholder skeleton，而会显示真实 `independent` 业务候选。

### 2. 净边际门槛

`independent` 的开仓/加仓现在会显式检查：

- `expected_net_edge_bps >= min_safe_net_edge_bps + slippage_buffer + execution_buffer`

如果不满足，会阻断对应腿：

- `independent_long_book_expected_net_edge_below_safe_threshold`
- `independent_short_book_expected_net_edge_below_safe_threshold`

### 3. 成本门槛

如果配置了 `strategy_hedge_independent_max_acceptable_cost_bps > 0`，开仓/加仓还会检查：

- `expected_cost_bps <= max_acceptable_cost_bps`

不满足时会阻断对应腿：

- `independent_long_book_expected_cost_above_max_acceptable`
- `independent_short_book_expected_cost_above_max_acceptable`

### 4. Hysteresis

`independent` 不再用 entry threshold 同时做开仓和退出。

现在逻辑是：

- `score >= scale_in_threshold`：允许加仓
- `entry_threshold > score >= close_threshold`：继续持有
- `score < close_threshold`：才进入退出判定

这会减少边界抖动造成的频繁开平。

## 未做的部分

本批次明确没有做：

- allocator 选主切流
- apply/execution 主路径切流
- 真实 executor 的 passive-first 执行迁移
- control plane / execution plane 的最终统一

这些属于后续 batch。
