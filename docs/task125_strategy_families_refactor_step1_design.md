# Task125：Strategy Families 重构 Step1 设计与迁移方案

## 1. 文档目标

本文档是本次重构的 Step 1 交付，不直接修改业务实现，只完成：

- 当前调用链与 family 身份现状梳理
- 新 family registry / interface / candidate schema / state machine 设计
- 分批迁移方案与回滚边界定义

本次重构不是简单拆文件，而是把 `protective / opportunistic / independent` 从 `directional` 的内部 hedge-path 升级为真正的顶层 `strategy families`，并同时修复：

- family identity 不真实
- top-level control plane 与 leg-level execution plane 语义分裂
- `independent` 交易资格设计弱于旧 `directional`
- audit / persistence / replay 对三条线的表达不一致

## 2. 当前调用链与 family 身份现状

### 2.1 当前决策主链

当前主链起点仍是 `directional`：

1. [target_position.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/target_position.py) 先走旧 `directional target` 逻辑  
   关键函数：
   - `_target_quantity()`
   - `_apply_position_management()`
   - `_position_intent()`
   - `_decision_outcome()`
2. 当满足 `hedge mode` 运行条件时，再在 `directional` 内部进入：
   - `_hedge_mode_strategy_legs()`
   - `_protective_overlay_decision()`
   - `_opportunistic_overlay_decision()`
   - `_independent_books_strategy_legs()`
   - `_independent_book_decision()`
   - `_independent_book_score()`
3. 最终由：
   - `_build_hedge_execution_leg()`
   - `_build_independent_execution_leg()`
   生成 `strategy_execution_legs`

当前本质是：

- 顶层仍先做 `directional target`
- `protective / opportunistic / independent` 只是内部派生分支
- execution 通过 `strategy_execution_legs` 真正落到腿级下单

### 2.2 当前 coordinator / allocator 如何看待 family

当前 family 在 schema 和调度层都只有 4 个：

- `directional`
- `smart_arbitrage`
- `spot_grid`
- `dca`

证据：

- [settings.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/settings.py)
- [strategy_runtime.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/schemas/strategy_runtime.py)

当前 [coordinator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/coordinator.py) 的行为是：

1. `evaluate()` 只直接生成 4 个顶层 candidate：
   - `_directional_candidate(directional_target)`
   - `smart_arbitrage_engine.evaluate(...)`
   - `spot_grid_engine.evaluate(...)`
   - `dca_engine.evaluate(...)`
2. `_directional_candidate()` 会把 `PositionTarget.strategy_execution_legs` 原样打包进 `directional` candidate
3. `_select_candidate()` 的优先顺序只有：
   - `smart_arbitrage`
   - `spot_grid`
   - `dca`
   - `directional`

当前 [allocator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/allocator.py) 也只围绕这 4 个 family 做：

- 冲突解决
- 预算分配
- selected family 选主

结论：

- `protective / opportunistic / independent` 当前还不是真正的 coordinator/allocator 一级 family
- 它们只是 `directional` candidate 附带出来的腿级语义

### 2.3 当前 execution / persistence 落地链

当前关键落地链在 [config.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/config.py) 的 `handle_position_target(...)`：

1. 收到 `topics.POSITION_TARGETS`
2. 如果 `target.strategy_execution_legs` 非空，进入腿级分支
3. 逐腿做：
   - `_strategy_leg_target(...)`
   - `policy_engine.evaluate(...)`
   - `_plan_for_strategy_leg(...)`
   - `risk_engine.evaluate_leg_order(...)`
4. 聚合后写：
   - `topics.POLICY_DECISIONS`
   - `topics.RISK_DECISIONS`
   - `topics.EXECUTION_PLANS`
   - `topics.ORDER_INTENTS`
   - `topics.STRATEGY_EXECUTION_BUNDLES`
   - `topics.DECISION_OUTCOMES`

当前 bundle/persistence 的关键问题：

- [StrategyExecutionBundle.family](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/schemas/strategy_runtime.py) 取的是 `target.strategy_family`
- 但参与执行的真实腿 family 又来自 `strategy_execution_legs[*].family`
- 结果会出现：
  - 顶层 bundle / target / outcome 看起来是 `directional`
  - 腿级实际跑的是 `protective / opportunistic / independent`

### 2.4 当前已确认的结构性问题

#### 问题 A：family identity 不真实

- `protective / opportunistic / independent` 没有自己的顶层 candidate identity
- coordinator / allocator / snapshot / audit 仍然首先把它们看成 `directional`

#### 问题 B：control plane 与 execution plane 分裂

当前已经观察到真实场景：

- top-level `target_position_qty = 0`
- top-level `position_intent = hold`
- `decision_outcome.final_action = hold`
- 但 `strategy_execution_legs` 在真实产生 `open_short`

这说明 operator 看到的是“观望”，execution 却在下单。

#### 问题 C：`independent` 缺少显式交易资格门槛

当前 `independent` 主要依赖：

- `_independent_book_score()`
- `_independent_book_decision()`

但没有像旧 `directional` 那样显式要求：

- `expected_gross_edge_bps`
- `expected_cost_bps`
- `expected_slippage_bps`
- `expected_net_edge_bps > 0`

#### 问题 D：`independent` 缺少真正的 hysteresis

当前实盘行为已经证明：

- 开仓线与平仓线长期围绕同一 entry threshold
- 容易在边界附近来回开平
- 形成弱边际 + 高频 churn

#### 问题 E：默认 execution 太贵

当前 planner 默认仍偏向：

- `taker`
- `market`
- `IOC`

弱 edge 场景默认走最贵执行，对 `independent` 明显不成立。

## 3. 新架构设计

### 3.1 顶层 family 集合

重构后 family 必须扩展为：

- `directional`
- `smart_arbitrage`
- `spot_grid`
- `dca`
- `protective`
- `opportunistic`
- `independent`

这里有 3 个硬约束：

1. `protective / opportunistic / independent` 必须进入顶层 `StrategyFamily`
2. coordinator / allocator / snapshot / audit 必须直接认识它们
3. 不能再靠“directional + legs”去伪装它们的 family identity

### 3.2 新 family registry

建议新增统一 registry：

```python
class StrategyFamilyEngine(Protocol):
    family_name: StrategyFamily

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> list[StrategyCandidate]:
        ...
```

建议新增：

- `StrategyEvaluationContext`
  - 基于当前 `StrategyEngineInput` 扩展
  - 加入 parent exposure / runtime feature flags / shadow mode / recent family snapshots
- `StrategyFamilyRegistry`
  - 统一注册 family engine
  - coordinator 只负责 dispatch，不再内嵌 family 特判

registry 目标：

- family 注册显式化
- family enable / shadow / live 执行 gating 集中化
- coordinator 不再手写固定 4 个 family 的装配逻辑

### 3.3 新 candidate schema

当前 `StrategyCandidate` 还不够表达三条新 family，需要扩展为 family-level candidate，而不是净仓候选。

建议新增或扩展字段：

- `family_action`
  - `hold_family`
  - `blocked`
  - `protect`
  - `rebalance_protection`
  - `open_opportunity_leg`
  - `close_opportunity_leg`
  - `open_independent_book`
  - `scale_independent_book`
  - `close_independent_book`
- `thesis`
  - 文本化说明 family 级交易意图
- `expected_gross_edge_bps`
- `expected_cost_bps`
- `expected_slippage_bps`
- `expected_buffer_bps`
- `expected_net_edge_bps`
- `gating_results`
  - 用于表达 expectancy / cooldown / parent exposure / execution gating 等
- `required_parent_exposure_id`
  - `protective / opportunistic` 必需
- `execution_intent_summary`
  - top-level 可解释摘要
- `risk_budget_required`

设计原则：

- `score` 只能表示 conviction / ranking
- `expected_net_edge_bps` 才能承担交易资格门槛

### 3.4 新 leg-level execution plan 语义

当前 `StrategyLegIntent` 还不够表达新 family 需要的执行资格与角色。

建议扩展：

- `leg_role`
  - `main`
  - `protective`
  - `opportunistic`
  - `independent_long`
  - `independent_short`
- `max_acceptable_cost_bps`
- `expected_leg_cost_bps`
- `execution_style_preference`
- `weak_edge_mode`

设计原则：

- family-level action 负责表达“为什么要做”
- leg-level action 负责表达“具体怎么做”
- top-level summary 不能再偷懒只靠净仓 target

### 3.5 新 control plane 语义

必须显式区分三层：

1. family-level action  
   例：
   - `protect`
   - `open_opportunity_leg`
   - `open_independent_book`
   - `hold_family`
   - `blocked`

2. leg-level action  
   例：
   - `open_short`
   - `scale_in_long`
   - `reduce_long`
   - `close_short`

3. net exposure summary  
   只能作为摘要，不能代替 family action

重构后的强约束：

- 不允许再出现 family-level `hold` 但 leg-level `open`
- `decision_outcome` / `position_target` / `bundle` / `order_intent` / `audit` 必须描述同一个事实

### 3.6 三条 family 的状态机设计

#### protective

- `inactive`
- `eligible`
- `open`
- `rebalance`
- `hold`
- `reduce`
- `close`
- `blocked`

约束：

- 仍依附主腿暴露
- 无主腿时不得裸开

#### opportunistic

- `inactive`
- `armed`
- `open`
- `scale`
- `hold`
- `unwind`
- `blocked_by_fee_drag`
- `blocked_by_churn`
- `closed_with_main_leg`

约束：

- 仍依附主腿持有期
- 本次不引入首个 directional entry 同周期 co-open

#### independent

- `no_position`
- `entry_candidate`
- `open`
- `scale_in`
- `hold`
- `de_risk`
- `close_candidate`
- `closed`
- `cooldown`
- `blocked`

新增硬约束：

- 必须引入 expectancy gating
- 必须引入 hysteresis
- 必须引入弱 edge execution gating

### 3.7 `independent` 的最低设计标准

新 `independent` 的交易资格设计不得弱于旧 `directional`。

必须新增：

- `expected_gross_edge_bps`
- `expected_fee_bps`
- `expected_slippage_bps`
- `expected_buffer_bps`
- `expected_net_edge_bps`

并显式要求：

- `expected_net_edge_bps > strategy_hedge_independent_min_safe_net_edge_bps`

同时引入明确 hysteresis：

- `close_threshold < entry_threshold < scale_in_threshold`

同时引入 execution gating：

- weak-edge 默认禁止 `taker + market + IOC`
- 支持 `passive-first / bounded limit / max acceptable cost`

## 4. 分批迁移方案

### Batch A：family registry + coordinator 骨架

目标：

- 扩展 `StrategyFamily`
- 引入 `StrategyFamilyEngine` 和 `StrategyFamilyRegistry`
- 让 coordinator 能调度 `protective / opportunistic / independent`
- 先允许 family engine 返回空 candidate，占位接入 snapshot / audit

涉及模块：

- `aats/bootstrap/settings.py`
- `aats/schemas/strategy_runtime.py`
- `aats/services/strategy_engines/base.py`
- `aats/services/strategy_engines/coordinator.py`
- 新增 `aats/services/strategy_engines/families/registry.py`
- 新增 3 个 family engine 骨架文件

验收标准：

- snapshot / audit 能识别 7 个 family
- legacy path 仍然可运行
- 不直接触碰旧 `target_position.py` 主逻辑

回滚方式：

- 关闭 `strategy_family_*_enabled`
- coordinator 回退 legacy family 集合

### Batch B：protective family

目标：

- 把 `_protective_overlay_decision()` 和相关 gating 从 `target_position.py` 迁出
- 形成独立 `protective family engine`
- 业务语义保持不变

必须保留：

- 依附主腿暴露
- 无主腿不得裸开

验收标准：

- coordinator / snapshot / audit 真正识别 `family="protective"`
- 不再仅通过 directional legs 才能看出 protective

回滚方式：

- 关闭 `strategy_family_protective_enabled`
- 回退 legacy protective path

### Batch C：opportunistic family

目标：

- 把 `_opportunistic_overlay_decision()` 和 fee drag / churn / parent exposure gating 迁出
- 形成独立 `opportunistic family engine`

必须保留：

- 依附主腿持有期
- 本次不引入 co-open
- 无主腿时仍 inactive / blocked

验收标准：

- family identity 独立
- control plane 明确表达机会腿动作
- legacy 行为不变

回滚方式：

- 关闭 `strategy_family_opportunistic_enabled`
- 回退 legacy opportunistic path

### Batch D：independent family

目标：

- 把 `independent` 从 directional 内部 hedge path 升级为真正顶层 family

必须新增：

- expectancy gating
- hysteresis
- execution gating
- family-level action 与 leg-level action 统一语义

建议新文件：

- `aats/services/strategy_engines/families/independent_family.py`

建议内部函数：

- `_build_book_context()`
- `_compute_expected_gross_edge_bps()`
- `_compute_expected_cost_bps()`
- `_compute_expected_slippage_bps()`
- `_compute_expected_net_edge_bps()`
- `_independent_expectancy_gate()`
- `_independent_book_state_transition()`
- `_build_independent_candidate()`
- `_build_independent_execution_legs()`

重要约束：

- `_independent_book_score()` 允许保留，但只能降级为 conviction / ranking
- 不再允许它单独决定交易资格

验收标准：

- `family="independent"`
- family-level action 与 execution legs 一致
- `PositionTarget / DecisionOutcome / Bundle / Audit` 语义一致
- replay / paper / shadow 下可观测

回滚方式：

- 关闭 `strategy_family_independent_live_execution_enabled`
- 再关闭 `strategy_family_independent_enabled`
- 回退旧 directional internal path

### Batch E：legacy path 切流与清理

目标：

- family-by-family 切换到新 family engine
- 旧 directional 内嵌 protective / opportunistic / independent path 进入只读 shadow
- 最终删除 legacy path

切流顺序：

1. protective
2. opportunistic
3. independent

必须先后顺序：

- shadow
- replay / paper
- 极小 live pilot
- family-by-family 切流
- 清理 legacy path

## 5. 配置设计

建议新增：

### family enable / shadow / live

- `strategy_family_protective_enabled`
- `strategy_family_opportunistic_enabled`
- `strategy_family_independent_enabled`
- `strategy_family_protective_shadow_mode_enabled`
- `strategy_family_opportunistic_shadow_mode_enabled`
- `strategy_family_independent_shadow_mode_enabled`
- `strategy_family_independent_live_execution_enabled`

### independent expectancy gating

- `strategy_hedge_independent_min_safe_net_edge_bps`
- `strategy_hedge_independent_expected_slippage_buffer_bps`
- `strategy_hedge_independent_expected_execution_buffer_bps`

### independent hysteresis

- `strategy_hedge_independent_long_entry_threshold`
- `strategy_hedge_independent_long_close_threshold`
- `strategy_hedge_independent_long_scale_in_threshold`
- `strategy_hedge_independent_short_entry_threshold`
- `strategy_hedge_independent_short_close_threshold`
- `strategy_hedge_independent_short_scale_in_threshold`

### independent execution gating

- `strategy_hedge_independent_weak_edge_execution_mode`
- `strategy_hedge_independent_max_acceptable_cost_bps`
- `strategy_hedge_independent_passive_first_enabled`

## 6. Step 1 之后的实现边界

在 Batch A 开始前，以下内容仍然禁止：

- 直接把三条线从 `target_position.py` 粗暴搬走
- 在没有新 family registry 和 schema 的前提下硬改 coordinator
- 在没有 expectancy gating 和 hysteresis 设计前直接改 `independent` 为 live 可用
- 擅自修改 `smart_arbitrage / spot_grid / dca` 主业务逻辑

## 7. 建议的下一批落地顺序

下一批只做 Batch A，不做业务语义迁移：

1. 扩展 `StrategyFamily`
2. 新增 family registry / protocol / context
3. coordinator 支持注册式 family evaluation
4. snapshot / audit 支持新 family identity
5. 新增 3 个 family engine 占位实现
6. 新增 enable / shadow / live flags

此阶段目标只有一个：

- 先让架构容器成立
- 旧逻辑继续跑
- 不提前混入 protective / opportunistic / independent 的业务迁移

## 8. Step 1 结论

当前系统的核心问题不是“几个 overlay 函数写得乱”，而是：

- family identity 不真实
- control plane 与 execution plane 分裂
- `independent` 缺少交易资格设计
- persistence / audit 把真实策略动作压回 `directional`

因此本次重构必须按架构迁移处理，而不是按普通代码整理处理。

