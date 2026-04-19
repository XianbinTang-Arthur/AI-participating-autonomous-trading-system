# Task 99 - 合约 Protective Overlay Phase 7 交付说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 交付目标

本阶段完成 `task91_derivatives_hedge_mode_phase_breakdown.md` 里 `Phase 7 protective` 的第一版落地：

- 在合约 `hedge mode` 下，让 directional 不再只输出净仓位，而是显式输出腿级执行目标。
- 在主腿已经存在库存时，支持 `protective overlay` 按压力分数打开保护腿。
- 把保护腿的最小持有时间、重平衡冷却、运行域支持情况，暴露到 runtime/operator/UI。

本阶段不做 `opportunistic` 和 `independent`，也不改现货。

## 2. 本次改动范围

### 2.1 策略与数据模型

- `aats/schemas/decision.py`
  - `DecisionContext` 新增 long/short 腿的 opened/closed/fill 时间戳。
  - `PositionTarget` 新增 `hedge_overlay_decision`。
- `aats/schemas/strategy_runtime.py`
  - `StrategyLegIntent` 新增 `position_mode / pos_side / action / overlay_mode / hedge_ratio / trigger_reason_codes`。
- `aats/services/decision_engine/context_builder.py`
  - 从 fill 历史重建 long/short 腿生命周期。
- `aats/services/decision_engine/target_position.py`
  - hedge mode 下 directional 统一产出显式主腿。
  - protective overlay 根据压力分数生成保护腿。
  - 最小持有和重平衡冷却会阻断保护腿调整。

### 2.2 执行接线

- `aats/bootstrap/config.py`
  - 对 `strategy_execution_legs` 优先走 `build_leg_plan / build_leg_intent`，不再把显式腿订单偷转回净仓语义。
- `aats/services/strategy_engines/coordinator.py`
  - 保留 directional 自己生成的 `strategy_execution_legs`，不再被协调器无意丢掉。

### 2.3 Operator / UI

- `aats/services/operator/query_service.py`
  - directional 配置里暴露 `hedge_overlay_*`。
- `aats/api/static/modules/views/strategy-view.js`
  - 策略页新增保护性对冲状态、阈值、比例、阻断原因展示。
- `aats/api/static/modules/terms.js`
  - 新增 protective overlay 相关中文映射。

## 3. 运行语义

### 3.1 当前开放的 overlay 模式

- `strategy_hedge_overlay_enabled=true`
- `strategy_hedge_overlay_mode=protective`

只有同时满足以下条件时才真正生效：

- 运行域是 `derivatives`
- `margin_mode != cash`
- `derivatives_position_mode == hedge`

### 3.2 当前策略行为

- 如果 directional 本轮主腿目标是多头：
  - 主腿会生成 `buy + long`
  - 若保护条件满足，会额外生成 `sell + short`
- 如果 directional 本轮主腿目标是空头：
  - 主腿会生成 `sell + short`
  - 若保护条件满足，会额外生成 `buy + long`

### 3.3 保护腿阻断规则

- `strategy_hedge_min_hold_seconds`
  - 保护腿刚开后，最小持有时间内不允许提前收口。
- `strategy_hedge_rebalance_cooldown_seconds`
  - 保护腿刚调整过后，冷却时间内不允许再次来回重平衡。

## 4. 配置项

已在合约 profile 中补齐：

- `strategy_hedge_overlay_enabled`
- `strategy_hedge_overlay_mode`
- `strategy_hedge_open_threshold`
- `strategy_hedge_close_threshold`
- `strategy_hedge_max_ratio`
- `strategy_hedge_min_hold_seconds`
- `strategy_hedge_rebalance_cooldown_seconds`

当前只建议在合约 `hedge` 运行线打开。

## 5. 测试策略

### 5.1 单元测试

- `tests/unit/test_target_position_engine.py`
  - hedge mode 主腿显式输出
  - protective overlay 开保护腿
  - 最小持有阻断提前收口
  - 重平衡冷却阻断重新打开
- `tests/unit/test_decision_context_builder.py`
  - long/short 腿生命周期时间戳重建

### 5.2 窄集成测试

- `tests/integration/test_strategy_runtime_integration.py`
  - `/strategy/runtime` 暴露 overlay 配置和显式腿语义
- `tests/integration/test_dashboard_ui.py`
  - 策略页展示 protective overlay 的状态和阈值说明

## 6. 兼容性与回滚

- 现有 net mode 行为保持不变。
- 只有 `derivatives_position_mode == hedge` 的运行线会走这套显式腿输出。
- 如需回滚：
  1. 关闭 `strategy_hedge_overlay_enabled`
  2. 把 `derivatives_position_mode` 保持在 `net`
  3. 如需完全回退，再撤掉 directional hedge-mode legs 路径

## 7. 剩余风险

- 这还是 `Phase 7 protective`，不包含 `opportunistic / independent`。
- `protective pressure` 当前是启发式评分，不是单独训练出来的风险模型。
- 如果后续要做更激进的双腿交易，必须另开后续阶段，不应在本阶段上直接放开。
