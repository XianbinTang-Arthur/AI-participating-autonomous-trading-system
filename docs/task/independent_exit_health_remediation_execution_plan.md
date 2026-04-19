# Independent Exit / Health Remediation Execution Plan

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 目标与边界

本文把当前已确认的问题整理成可执行实施版，覆盖：

- independent `target_qty=0` full-close exit 被 allocator 误缩量
- execution health 以 fill 为样本单位，偏离真实 lifecycle 语义
- health guard 无恢复语义，容易长时间 fail-closed
- symbol-level raw guard 与 leg-level guard-eligible guard 口径分裂
- independent entry gating 对弱信号放行过宽

当前已确认的动态边界：

- live 库中 `current_qty != 0 && target_qty == 0` 的 sleeve intent 共 20 笔
- 全部来自 independent
- 分布为：
  - `13` 笔 `close_failed_thesis_independent_book`
  - `7` 笔 `de_risk_independent_book` 且已推进到 full close
- `close_stale_thesis_independent_book` 当前 live 样本为 `0`，但代码路径与 `close_failed_thesis` 同类，按高置信纳入修复范围

不在本批范围内：

- smart_arbitrage / directional / protective 的参数优化
- operator UI 展示改版
- 长周期 live 参数最优校准

## 2. Batch 1

### Task 1.1 修复 independent full-close 的 requested_notional 估值

目标：

- independent 在 `target_qty=0` 的 exit 上，不再退化到 `max_symbol_notional`

核心改动：

- 为 independent full-close 优先使用“当前持仓 notional / qty”语义估值
- 当 leg intent / base target 已有足够价格语义时，禁止 fallback 到 `effective_max_symbol_notional`
- 明确 `close_failed_thesis`、`close_stale_thesis`、`de_risk_floor_promoted_to_close` 三类都走同一 full-close 估值路径

文件清单：

- [aats/services/strategy_engines/allocator.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/allocator.py>)
- [aats/services/strategy_engines/coordinator.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/coordinator.py>)
- [aats/services/decision_engine/target_position.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/target_position.py>)

单测：

- `tests/unit/test_strategy_coordinator.py::test_independent_full_close_failed_thesis_allocation_target_stays_zero`
- `tests/unit/test_strategy_coordinator.py::test_independent_full_close_stale_thesis_allocation_target_stays_zero`
- `tests/unit/test_strategy_coordinator.py::test_independent_full_close_derisk_promoted_to_close_allocation_target_stays_zero`
- `tests/unit/test_strategy_coordinator.py::test_independent_full_close_requested_notional_does_not_fallback_to_max_symbol_notional`

集成验证点：

- `StrategySleeveIntent(target_qty=0)` -> `PortfolioAllocationDecision(target_qty=0)` -> `PositionTarget(target_qty=0)` 全链保持一致
- `budget_snapshots.requested_notional != max_symbol_notional`
- `clamped=false` 或即使 `clamped=true` 也不能把 full-close 改成 partial close

回归风险：

- 误伤 normal partial reduce
- 误伤 reversal / smart_arbitrage 的 gross-notional 估值逻辑

### Task 1.2 给 independent leg intent 补 reference_price / projected_notional

目标：

- 让 allocator 不再依赖 base target 的零值 notional

核心改动：

- 在 independent leg 构造处补齐 `reference_price`
- 条件允许时补齐 `projected_notional`
- 确保 `close_failed_thesis` / `close_stale_thesis` / `de_risk` 共享同一腿级价格语义

文件清单：

- [aats/services/strategy_engines/families/independent_family.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/families/independent_family.py>)

单测：

- `tests/unit/test_independent_family.py::test_build_independent_leg_sets_reference_price_for_close_failed_thesis`
- `tests/unit/test_independent_family.py::test_build_independent_leg_sets_reference_price_for_close_stale_thesis`
- `tests/unit/test_independent_family.py::test_build_independent_leg_sets_reference_price_for_derisk_close_path`

集成验证点：

- `strategy.sleeve_intents` payload 中 independent `legs[0].reference_price` 不再为 `null`
- allocator `_requested_notional()` 能直接从 legs 估值，不进入 `effective_max_symbol_notional` fallback

回归风险：

- reference price 来源不稳定时，可能影响 replay / operator 观测值

## 3. Batch 2

### Task 2.1 将 execution health 样本单位从 fill 改为真实 lifecycle

目标：

- 一个真实持仓生命周期只记一个 health 样本

核心改动：

- 引入 lifecycle 级 close outcome 聚合
- `fee_drag / churn / win_rate / low_edge` 全部基于 lifecycle outcome 计算
- 保留 residual exclusion，但 exclusion 的对象变成 lifecycle 或 lifecycle fragment attribution，而不是单 fill 主导

文件清单：

- [aats/services/strategy_execution_health.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_execution_health.py>)
- [aats/services/strategy_execution_guard_filters.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_execution_guard_filters.py>)
- [aats/services/operator/lifecycle_attribution.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/lifecycle_attribution.py>)

单测：

- `tests/unit/test_strategy_execution_health.py::test_fragmented_close_fills_collapse_into_single_lifecycle_outcome`
- `tests/unit/test_strategy_execution_health.py::test_small_churn_is_computed_from_lifecycle_not_fill`
- `tests/unit/test_strategy_execution_health.py::test_guard_eligible_counts_use_lifecycle_after_residual_filtering`
- `tests/unit/test_operator_lifecycle_attribution.py::test_execution_health_sampling_matches_operator_lifecycle_unit`

集成验证点：

- `开仓 -> 多次部分平仓 -> 回到 0` 只形成 `1` 个 recent closed trade
- 之前 short 14 笔退出的场景，不再得到 `recent_closed_trade_count=12 / guard_eligible=9`

回归风险：

- operator / dashboard 中 health 指标口径变化
- 历史值会跳变，需要在运维文档说明

### Task 2.2 为 health guard 增加恢复语义

目标：

- 避免 count-only lookback 把系统锁死数小时

核心改动：

- 引入“时间窗 + 数量窗”或时间衰减
- 明确 operator reset 的管理路径
- guard 只对近期样本生效，不让昨天/更早样本无限期支配今天

文件清单：

- [aats/services/strategy_execution_health.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_execution_health.py>)
- [aats/bootstrap/settings.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/settings.py>)
- [configs/strategy_profiles/derivatives_live.yaml](</D:/文件/project/AIParticipatingAutonomousTradingSystem/configs/strategy_profiles/derivatives_live.yaml>)

单测：

- `tests/unit/test_strategy_execution_health.py::test_health_guard_recovers_after_lookback_window_expires`
- `tests/unit/test_strategy_execution_health.py::test_guard_eligible_metrics_decay_without_new_bad_lifecycles`
- `tests/unit/test_task63_trial_guard.py::test_trial_guard_respects_new_health_window`

集成验证点：

- 在无新增坏样本时，guard 指标能自然回落
- `trial_guard / execution_health_not_ok / churn_guard` 不再全天持续锁死

回归风险：

- 时间窗过短会放松过度
- reset 能力必须有审计记录

## 4. Batch 3

### Task 3.1 统一 guard source of truth

目标：

- 消除 symbol-level raw guard 与 leg-level guard-eligible guard 的分裂

核心改动：

- 明确 independent 的 governing guard 只认 leg-level guard-eligible
- symbol-level raw 指标改为观测用途，或同步切换到 guard-eligible 后再决定是否保留阻断能力
- 修复 `decision_outcome blocked` 但后续仍被 `override_target` 覆盖的路径

文件清单：

- [aats/services/decision_engine/context_builder.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/context_builder.py>)
- [aats/services/decision_engine/target_position.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/target_position.py>)
- [aats/services/strategy_engines/coordinator.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/coordinator.py>)
- [aats/services/strategy_engines/independent/gates.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/independent/gates.py>)

单测：

- `tests/unit/test_target_position_engine.py::test_independent_entry_block_uses_guard_eligible_leg_health`
- `tests/unit/test_strategy_coordinator.py::test_symbol_level_blocker_is_not_overridden_by_independent_allocation`
- `tests/unit/test_independent_gates.py::test_performance_degraded_prefers_guard_eligible_metrics`

集成验证点：

- 不再出现 `decision_outcome` 含 `execution_churn_guard_active` 但最终 `enter`
- short leg `closed_trade_count=0` 时，不再被 symbol-level raw blocker 和 leg-level allow 互相打架

回归风险：

- 影响现有 operator 审计字段解释
- 需要同步更新 operator API / dashboard 口径说明

### Task 3.2 independent entry gating 保护性收紧

目标：

- 先消除“弱信号刚好踩线放行”

核心改动：

- 恢复 `min_confirm_ticks`
- 收紧 independent long/short entry threshold 到保守区间
- 复核 short scoring 结构性加分项是否过宽

文件清单：

- [configs/strategy_profiles/derivatives_live.yaml](</D:/文件/project/AIParticipatingAutonomousTradingSystem/configs/strategy_profiles/derivatives_live.yaml>)
- [aats/services/strategy_engines/independent/scoring.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/independent/scoring.py>)
- [aats/services/strategy_engines/independent/gates.py](</D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/independent/gates.py>)

单测：

- `tests/unit/test_independent_scoring.py::test_short_breakout_weak_signal_no_longer_crosses_entry_threshold`
- `tests/unit/test_independent_gates.py::test_short_entry_requires_min_confirm_ticks_after_guard_tightening`
- `tests/unit/test_env_profiles.py::test_derivatives_live_independent_entry_thresholds_are_retightened`

集成验证点：

- 类似 `decision_d4c8...` 的弱 short 场景不再通过 entry gate
- 收紧后不影响已持仓正常 close / reduce / recovery

回归风险：

- 只能解释为保护性收紧，不能解释为长期最优参数

## 5. 建议执行顺序

### Batch 1

- Task 1.1 allocator full-close requested_notional 修复
- Task 1.2 independent leg reference_price / projected_notional 补齐

目标：

- 先修掉确定性逻辑 bug，保证 full-close 真的能 close

### Batch 2

- Task 2.1 execution health lifecycle sampling
- Task 2.2 health recovery 语义

目标：

- 修掉 guard 被碎片退出喂满并长时间自锁的问题

### Batch 3

- Task 3.1 guard source-of-truth 统一
- Task 3.2 entry gating 保护性收紧

目标：

- 最后再收敛治理口径和入场行为，避免用参数掩盖上游缺口

## 6. 批次验收建议

Batch 1 验收：

- full-close exit 不再被 allocator 缩成 partial close
- `close_failed_thesis` / `close_stale_thesis` / promoted close 都覆盖到

Batch 2 验收：

- health 样本单位与 lifecycle 语义一致
- 无新增坏样本时 guard 能恢复

Batch 3 验收：

- guard source-of-truth 唯一
- 弱信号 entry 明显减少
- close / reduce 安全链路无回归
