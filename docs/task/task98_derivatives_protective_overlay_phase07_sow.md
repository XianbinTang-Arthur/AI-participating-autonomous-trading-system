# Task 98：合约 Hedge Mode Phase 7 Protective Overlay 实施说明

## 1. 业务目标与边界

- 目标：在 `derivatives + hedge` 运行域里，为 directional 主链增加第一版可实跑的 `protective` overlay。
- 本阶段只开放 `protective`，不开放 `opportunistic / independent`。
- 主腿继续由 directional 主信号决定；保护腿只在保护性条件成立时开启或回收。
- 不改现货主链，不改 smart_arbitrage 策略语义，不改 public API 的既有字段含义。

## 2. 模块职责与领域模型

- `aats/services/decision_engine/context_builder.py`
  - 为 directional hedge overlay 提供腿级开仓/平仓时间与最近腿级成交时间。
- `aats/services/decision_engine/target_position.py`
  - 继续负责 directional 主腿目标。
  - 在 `hedge` 运行域里，把主腿目标翻译成双腿目标。
  - 在保护性条件成立时，附加保护腿目标与 overlay 决策摘要。
- `aats/bootstrap/config.py`
  - 把 directional hedge overlay 的 `strategy_execution_legs` 接到腿级执行链。
- `aats/services/operator/query_service.py`
  - 暴露 overlay 运行参数。
- `aats/api/static/modules/views/strategy-view.js`
  - 展示 overlay 配置与当前决策状态。

## 3. 输入 / 输出接口

- 新增运行参数：
  - `strategy_hedge_overlay_enabled`
  - `strategy_hedge_overlay_mode`
  - `strategy_hedge_open_threshold`
  - `strategy_hedge_close_threshold`
  - `strategy_hedge_max_ratio`
  - `strategy_hedge_min_hold_seconds`
  - `strategy_hedge_rebalance_cooldown_seconds`
- 新增决策输出：
  - `PositionTarget.hedge_overlay_decision`
- directional 在 `hedge` 运行域里输出：
  - `PositionTarget.strategy_execution_legs`

## 4. 状态一致性与生命周期

- `protective overlay` 只在 `derivatives_position_mode=hedge` 时生效。
- 保护腿的开启、持有、回收必须满足：
  - 最小持有时间
  - 再平衡冷却时间
- 旧的净仓位字段继续保留，但在 hedge overlay 下仅作为派生摘要，不再代表唯一执行真相。

## 5. 风控与恢复关系

- 本阶段不重写 Phase 4/5 的风控与恢复模型。
- 保护腿执行必须继续走 Phase 3/4 已有的腿级订单与腿级风控链。
- 若风险链拒绝保护腿，仍以显式 blocker 和 order state 体现，不允许静默回退到 net 下单。

## 6. 测试策略

- 单测：
  - protective overlay 开启条件
  - protective overlay 最小持有
  - protective overlay 再平衡冷却
  - hedge mode 下 directional 生成双腿执行目标
- 集成：
  - `PositionTarget -> strategy_execution_legs -> leg execution intent` 主链
  - `/strategy/runtime` 暴露 overlay 配置与最近目标摘要

## 7. 回滚与兼容

- 配置级回滚：
  - 关闭 `strategy_hedge_overlay_enabled`
  - 或把 `derivatives_position_mode` 维持为 `net`
- 代码级回滚：
  - 保留原 net target 语义与旧字段，不做破坏性删除

## 8. 验收标准

- `derivatives + hedge` 下，directional 能输出显式双腿执行目标。
- `protective` 条件下，系统会生成保护腿，而不是只剩净仓平仓语义。
- 保护腿满足最小持有与再平衡冷却约束。
- `/strategy/runtime` 和策略页能看到 overlay 配置与当前状态。
