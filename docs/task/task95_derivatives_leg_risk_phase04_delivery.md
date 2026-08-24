# Task 95: 合约对冲模式 Phase 4 腿级风控交付说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 目标与边界

本阶段对应 [Task 91](task91_derivatives_hedge_mode_phase_breakdown.md) 的 Phase 4，目标是把合约 `hedge mode` 的风控从单一净仓语义升级成：

- `long`
- `short`
- `gross`
- `net`

四口径并行评估，并把 `only_reduce_required` 从“整符号一刀切”升级成“按腿约束”。

本阶段不做：

- 策略层双腿 target 模型改造
- 腿级 reconciliation / recovery
- UI 页面双腿风险卡片

## 2. 当前行为总结

在本阶段之前：

- Phase 3 已支持显式 `LegOrderIntent`
- 但 `risk.py` 仍主要按 signed net target 做预交易风控
- `OrderManager.submit_leg_order()` 没有接上腿级风险评估
- `DerivativesLiveGuardService` 只暴露保证金和强平距离，不暴露 long/short/gross/net 风险口径

## 3. 本次改造

### 3.1 模块职责

- `aats/services/governance_engine/risk.py`
  - 新增四口径风险评估
  - 新增腿级 `only_reduce` 约束输出
  - 新增 `evaluate_leg_order()`
- `aats/services/execution_engine/order_manager.py`
  - 显式腿订单在提交前接入 `leg_risk_evaluator`
  - 风险拒绝时直接持久化 `BLOCKED`，不再触碰交易所
- `aats/services/governance_engine/derivatives_live_guard.py`
  - 补充当前 `long/short/gross/net` 暴露摘要

### 3.2 新增/调整的风险口径

- `risk_max_long_notional`
- `risk_max_short_notional`
- `risk_max_gross_notional`
- `risk_max_net_notional`

兼容策略：

- 这四个新配置默认值为 `0`
- 当值为 `0` 时，自动回退到旧配置：
  - `long/short/gross -> max_gross_notional_per_symbol`
  - `net -> max_notional_per_symbol`

这样不会因为未显式配置新阈值而意外收紧旧系统。

### 3.3 腿级 only-reduce 语义

当前实现遵循：

- 当前持有 `long` 时，如果是 `long` 腿超限，只锁 `long`
- 当前持有 `short` 时，如果是 `short` 腿超限，只锁 `short`
- `gross` 超限时，锁两条腿的继续扩张
- `net` 超限时，只锁当前净暴露方向
- 外部 `only_reduce` 来源（runtime guard / reconciliation / recovery）优先约束当前已持有腿；如果当前空仓，则两条腿都锁

这保证：

- 合法减仓不会被误伤
- 保护性对冲腿在不恶化当前净暴露时可以继续通过

### 3.4 显式腿订单接线

`OrderManager` 新增可选 `leg_risk_evaluator`：

- 仅在 `derivatives + hedge` 运行域装配
- 对 `submit_leg_order()` 的腿单先做风险评估
- 若风险拒绝：
  - 直接写入 `BLOCKED`
  - `submission_mode=leg_risk_blocked`
  - 不进入 adapter submit

## 4. 输入输出接口

### 输入

- `RiskEngine.evaluate(target: PositionTarget)`
- `RiskEngine.evaluate_leg_order(leg_intent: LegOrderIntent)`

### 输出

`RiskDecision` 现在会额外携带：

- `current_derivatives_exposure`
- `projected_derivatives_exposure`
- `derivatives_exposure_limits`
- `leg_only_reduce_constraints`

`DerivativesLiveGuardService.snapshot()` 现在额外携带：

- `current_derivatives_exposure.long_notional`
- `current_derivatives_exposure.short_notional`
- `current_derivatives_exposure.gross_notional`
- `current_derivatives_exposure.net_notional`
- `current_derivatives_exposure.gross_leverage`
- `current_derivatives_exposure.net_leverage`

## 5. 数据库、事务与一致性

本阶段没有新增表结构，也没有变更现有表约束。

一致性策略：

- 腿级风险拒绝发生在本地下单持久化入口之前
- 被拒绝的腿单仍保留一条 `BLOCKED` 订单状态，便于审计
- 不触碰交易所，因此不会出现“本地 blocked / 交易所已下单”的不一致

## 6. 安全、性能与审计

- 安全：风险拒绝优先于 adapter submit，避免 hedge mode 下显式腿单绕过风控
- 性能：当前四口径评估仅基于单次账户快照与本地聚合，未引入额外外部 I/O
- 审计：被风控拦截的腿单会留下 `BLOCKED` 状态与明确错误码

## 7. 测试策略

本阶段新增/覆盖：

- `tests/unit/test_guarded_live.py`
  - 同侧扩张被腿级 only-reduce 拦截
  - 保护性对冲腿不被误伤
- `tests/unit/test_task72_derivatives_live_guard.py`
  - runtime guard 暴露当前四口径摘要
- `tests/unit/test_order_manager_errors.py`
  - 腿级风险拒绝不会进入 adapter submit
- `tests/integration/test_okx_live_submit_path.py`
  - live futures 显式腿单路径在风险拒绝时直接本地 `BLOCKED`

## 8. 回滚与兼容

### 回滚

如需回滚本阶段，只需回退以下模块：

- `risk.py`
- `order_manager.py`
- `derivatives_live_guard.py`
- 对应测试

### 兼容

- 旧 net 风控入口 `evaluate(PositionTarget)` 继续保留
- 未配置新四口径阈值时，会自动沿用旧限额
- 仅 `derivatives + hedge` 会启用腿级下单风控接线

## 9. 剩余风险

- 本阶段仍未完成 Phase 5，对账与恢复还不是腿级语义
- `evaluate(PositionTarget)` 仍是 net target 主模型，对 hedge mode 只提供兼容性风险评估，不是最终策略模型
- runtime guard 当前暴露的是账户级汇总口径，跨多标的时 `net_notional` 更适合作为观察指标，而不是精确对冲度量

## 10. 验收标准

本阶段完成后，应满足：

- 显式腿单在本地下单前会经过四口径风险评估
- 同侧继续扩张会被腿级 only-reduce 正确阻断
- 保护性对冲腿在不恶化净暴露时可继续通过
- runtime guard 能暴露当前 `long/short/gross/net` 摘要
- 不修改配置时，旧测试与旧 net 路径行为保持兼容
