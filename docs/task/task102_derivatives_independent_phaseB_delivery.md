# Task 102：合约 Independent 双书状态机 Phase B 交付说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 背景

`Task 100` 已经完成 `Phase A: opportunistic`，但 `independent` 仍停留在配置枚举和任务书层面。
本阶段的目标，是把合约 `hedge mode` 下的 `independent` 从“可配置但未落地”，推进到“具备按腿决策、按腿冷却、按腿试盘守护和运行时可见性”的状态。

本阶段仍然只覆盖策略状态机和运行时配置暴露，不扩大到后续 `Phase C` 的 operator / UI 全链展示。

## 2. 当前行为

完成本阶段后，合约 `directional` 在满足以下条件时，会走 `independent` 路径：

- `trading_product_type=derivatives`
- `derivatives_position_mode=hedge`
- `strategy_hedge_overlay_enabled=true`
- `strategy_hedge_overlay_mode=independent`
- `strategy_hedge_independent_enabled=true`

在该模式下：

- long book 与 short book 按各自阈值独立决定开仓、持有、加仓、减仓和平仓
- 一条腿的 post-close cooldown、low-edge cooldown、trial guard、fee/churn 表现，不再直接污染另一条腿
- `DecisionContext` 会携带 `leg_strategy_health.long` 与 `leg_strategy_health.short`
- `/strategy/runtime` 会暴露 independent 配置及模式就绪状态

## 3. 主要改动

### 3.1 配置与校验

在 `aats/bootstrap/settings.py` 增加：

- `strategy_hedge_independent_enabled`
- `strategy_hedge_independent_long_entry_threshold`
- `strategy_hedge_independent_short_entry_threshold`
- `strategy_hedge_independent_long_scale_in_threshold`
- `strategy_hedge_independent_short_scale_in_threshold`
- `strategy_hedge_independent_long_min_hold_seconds`
- `strategy_hedge_independent_short_min_hold_seconds`
- `strategy_hedge_independent_rebalance_cooldown_seconds`
- `strategy_hedge_independent_trial_guard_enabled`

并增加阈值校验：

- threshold 必须在 `[0, 1]`
- `entry_threshold <= scale_in_threshold`

### 3.2 决策上下文

在 `aats/services/strategy_execution_health.py` 与 `aats/services/decision_engine/context_builder.py` 增加按腿健康度计算：

- long / short 各自的 closed trade 计数
- recent net realized pnl
- win rate
- fee drag
- churn
- low-edge streak

`DecisionContext` 新增 `leg_strategy_health`，供 independent 决策直接使用。

### 3.3 Independent 双书决策

在 `aats/services/decision_engine/target_position.py` 增加：

- `_independent_books_strategy_legs()`
- `_independent_book_decision()`
- 按腿的 min-hold、rebalance cooldown、post-close cooldown、low-edge cooldown、trial guard、fee/churn guard
- `independent_long_book` / `independent_short_book` 两类执行腿

该阶段的策略语义是：

- long book 与 short book 各自独立计算目标腿仓位
- 最终净仓位仍作为派生值输出给旧链路兼容字段
- 一条腿受阻时，另一条腿仍可在合法条件下独立调整

### 3.4 运行时可见性

在 `aats/services/operator/query_service.py`：

- `hedge_overlay_mode_ready` 新增对 `independent` 的就绪判断
- `/strategy/runtime` 的 `configured_parameters.directional` 增加 independent 参数暴露

## 4. 测试策略

本阶段新增 / 更新测试覆盖：

- `tests/unit/test_settings.py`
  - 校验 independent entry / scale-in 阈值约束
- `tests/unit/test_env_profiles.py`
  - 校验 derivatives / derivatives_live profile 已包含 independent 默认值
- `tests/unit/test_target_position_engine.py`
  - 校验 short 冷却不阻断 long 合法再入场
  - 校验 long 试盘守护变差不拖累 short 持仓
- `tests/unit/test_decision_context_builder.py`
  - 校验 builder 会把 long / short 的策略健康度分开计算
- `tests/integration/test_strategy_runtime_integration.py`
  - 校验 `/strategy/runtime` 暴露 independent 配置和腿级语义

## 5. 风险与边界

- 本阶段仍不是 `Phase C`，operator 审计与页面展示还没有完整扩成 independent 专属视图
- 当前 `StrategyLegIntent.role` 在 independent 下仍沿用兼容语义，没有把“long book / short book”建成新的公开 role 枚举
- 净仓位字段仍然保留，用于兼容旧执行链和查询链；真正的双书决策已经改为按腿计算
- 仓库仍存在历史 lint 存量问题，本阶段没有顺手清理

## 6. 验收结论

本阶段完成后，可以明确回答两类关键问题：

- short book 正在冷却时，long book 是否还能合法再入场：可以
- long book 的 trial guard 变差，是否会把 short book 一起关掉：不会

这意味着 `independent` 已经从“配置枚举”升级成“具备按腿状态机能力”的实际实现，但仍需在后续 `Phase C` 完成 operator / 审计 / UI 的体系化呈现。
