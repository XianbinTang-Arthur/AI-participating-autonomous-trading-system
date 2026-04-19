# Task 109：independent 二次审查后的补丁修复

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 目标

本任务只修复对 `independent` 再次 code review 后确认的 3 类问题：

1. 主链把腿级订单归一化成 `OrderIntent` 后，执行层新增的腿级 risk / rollout 防线没有真正生效。
2. `independent` 在 bundle 合并风控失败时仍然退回整包阻断，没有保留可安全执行的腿级子集。
3. fill 历史不完整时，腿级生命周期锚点可能随着新 snapshot 反复前移，导致 `min_hold` / `rebalance cooldown` 被长期卡住。

## 2. 边界

- 不新增新的 overlay 模式。
- 不改 public topic 名称。
- 不把运行时改成多模式并行。
- 只修复 `independent` 主链、执行边界和生命周期恢复语义。

## 3. 修复策略

### 3.1 执行层腿级防线补回主链

- 保持主链继续发布 `ORDER_INTENTS`，避免扩大 topic 改造范围。
- 在 `OrderManager.handle_order_intent()` 中，从携带完整腿字段的 `OrderIntent` 反推 `LegOrderIntent`。
- 只要反推成功，就继续走腿级：
  - `leg_risk_evaluator`
  - overlay rollout gate
  - adapter `submit_leg_order()`

### 3.2 bundle 风险失败时保留安全子集

- `independent` 不再把 bundle 合并风控失败简单等同于整包失败。
- 改为：
  - 先评估 full bundle
  - full bundle 失败后，按稳定顺序尝试构建“安全腿级子集”
  - 对被 bundle 风险挡掉的腿，写回明确的 `risk_rejection_reasons`
- 风险收敛顺序：
  - `close/reduce` 优先于 `open`
  - `open` 腿按 independent long/short score 高者优先
  - 同分时保持原始腿顺序

### 3.3 生命周期锚点稳定化

- `DecisionContextBuilder` 改为使用 scoped portfolio snapshot history。
- 当 fill 历史不足以重建当前腿数量时：
  - 优先使用最新 fill 时间
  - 否则回退到“当前连续持仓区间”的最早 snapshot 时间
  - 最后才回退到当前 snapshot 时间
- 这样避免每次新 snapshot 到来都把 opened_at 刷新成“刚开仓”。

## 4. 一致性要求

- 主链发布的 normalized `OrderIntent` 仍必须保留完整腿字段。
- `independent` 允许部分腿继续执行，但最终 `DecisionOutcome`、`StrategyExecutionBundle`、recovery/audit 需和真实执行子集一致。
- 所有新增行为都必须有单测或最窄集成测试覆盖。
