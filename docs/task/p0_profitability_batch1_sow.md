# P0 Profitability Batch 1 SoW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Scope

本批次只实现四项 P0 逻辑修复，不做长期参数调优：

1. full lifecycle cost gate
2. 残仓最小经济动作门槛
3. execution health 对退出残仓降敏
4. 主视图账单口径统一

## Objectives

- 让 independent / 15m 的开仓判断覆盖更保守的全生命周期成本，而不是只看单边 entry 成本。
- 避免残仓被继续切成很小的 taker 子单，放大 churn 和 fee drag。
- 让 execution health 在“已进入退出链的残仓阶段”不过度自激。
- 让主视图默认使用更接近交易所账单的综合净收益口径。

## Non-goals

- 不修改长期 live 参数取值。
- 不对 independent / 15m 做新的盈利参数优化。
- 不修改 public API 的既有字段语义。

## Design

### 1. Full lifecycle cost gate

- 保留现有 `expected_cost_bps` 语义，继续表示单边 entry 成本。
- 在 `IndependentBookExpectancy` 上新增：
  - `expected_lifecycle_cost_bps`
  - `expected_lifecycle_net_edge_bps`
- 开仓 gate 和高边际单 tick 放宽逻辑优先使用 lifecycle net edge。
- 成本异常 fuse 继续使用单边 `expected_cost_bps`，避免把现有 `max_acceptable_cost_bps` 静默改成 round-trip 语义。

### 2. 残仓最小经济动作门槛

- 在 independent book de-risk 路径上引入内部保护阈值。
- 当剩余仓位名义金额或本次 de-risk 动作名义金额低于内部经济下限时，直接收敛为整仓 close。
- 该阈值先作为内部逻辑常量，不新增 live 参数。

### 3. Execution health 对退出残仓降敏

- 在 `ClosedTradeOutcome` 增加 `is_residual_exit`。
- 对 recent health lookback 额外计算一组 guard-eligible 指标：
  - `recent_guard_eligible_closed_trade_count`
  - `recent_guard_eligible_win_rate`
  - `recent_guard_eligible_churn_ratio`
  - `recent_guard_eligible_low_edge_trade_streak`
  - `recent_guard_eligible_low_edge_trade_at`
- `execution_health_state`、performance guard、low-edge cooldown、trial guard 优先读取 guard-eligible 指标。
- `fee_drag_ratio` 继续保留 raw 口径，确保真实费耗不被掩盖。

### 4. 主视图账单口径统一

- strategy 主视图“最近净收益”切换为 `combined_net_realized_pnl`。
- 前向验证周期表头与主值统一改为“综合净收益”。
- 保留其他辅助口径，但不再把它们作为主盈利展示。

## Validation

- `.\.venv\Scripts\python.exe -m ruff check aats/ --fix`
- `.\.venv\Scripts\python.exe -m pytest tests/unit/ -x -q`
- WSL2 中运行最窄 dashboard UI 集成测试

## Acceptance

- 开仓 gate 在单边成本充足、但 lifecycle net edge 不足时会阻断开仓。
- de-risk 对小残仓不再继续生成低经济性 reduce 路径。
- execution health 在残仓退出场景下不会再仅因残仓 small churn 自激恶化。
- strategy 主视图主盈利口径与账单语义一致。
