# Task 100：合约 Opportunistic / Independent Overlay 扩展任务书

## 1. 任务背景

`Task 90` 和 `Task 91` 已经把合约 `hedge mode` 的底层能力拆解完成，`Task 99` 又交付了 `Phase 7 protective` 的第一版可实跑实现。

当前系统已经具备：

- 交易所 `posMode` 与本地 `derivatives_position_mode` 的显式契约
- 双腿仓位快照与腿级订单语义
- long / short / gross / net 四口径风控
- 腿级对账、恢复摘要和 operator 可见性
- directional 在 `derivatives + hedge` 下的 `protective overlay`

但当前仍然**只开放 `protective`**。`opportunistic` 和 `independent` 虽然已经出现在配置枚举和任务书目标里，但运行时尚未开放，现阶段仍应视为未实现能力。

本任务的目的，是把 `opportunistic` 和 `independent` 作为一个**独立新任务**立项，不与已经交付的 `protective` 实现混改。

## 2. 当前行为摘要

当前 `Phase 7 protective` 的真实行为如下：

- 只有在 `derivatives_position_mode == hedge` 时，directional 才会走显式腿输出。
- `strategy_hedge_overlay_mode=protective` 时，系统会基于压力分数决定是否打开一条保护腿。
- `strategy_hedge_min_hold_seconds` 和 `strategy_hedge_rebalance_cooldown_seconds` 已经生效。
- `/strategy/runtime`、operator 查询和策略页已经能展示 `hedge_overlay_*` 配置和本轮 `hedge_overlay_decision`。

当前仍未开放的部分：

- `opportunistic` 还没有独立的开平仓语义。
- `independent` 还没有真正的双账本策略状态机。
- 保护腿当前仍然以“主腿存在时的附属腿”为主语义，不是自治腿。
- 现有 `protective pressure` 评分不能直接拿来当 `opportunistic` 或 `independent` 的统一决策器。

一句话总结：

**现在系统支持“保护性对冲”，但还不支持“机会型对冲”和“长短两本书独立运行”。**

## 3. 本任务目标

本任务要把策略 overlay 从单一 `protective` 扩展成两条独立能力线：

1. `opportunistic overlay`
   - 在已有主腿时，允许系统基于短周期机会信号主动开立或增减对侧腿
   - 目标不是单纯防守，而是利用短线逆向波动、basis 偏移、流动性回补或事件后回抽机会
   - 仍然要求主腿与机会腿之间存在明确的上限关系，不允许失控扩张成双向大裸仓

2. `independent overlay`
   - long book 与 short book 具备各自独立的入场、加仓、减仓、平仓生命周期
   - 双腿不再要求“必须有主腿，才允许有副腿”
   - `net_qty` 彻底退化为派生指标，不能再成为独立策略关闭一侧腿的唯一依据

## 4. 非目标

本任务明确不做以下事项：

- 不给现货模式引入相同 overlay 能力
- 不跳过现有 `protective`，直接把其逻辑替换掉
- 不把 `opportunistic` 简化成“把 protective 阈值调松”
- 不把 `independent` 简化成“两条 protective 同时开”
- 不在没有回放样本与 operator 审计支持的前提下直接开放真实交易

## 5. 模式定义

### 5.1 Opportunistic

`opportunistic` 的定义是：

- 主腿仍然存在“主方向”概念
- 机会腿可以在主腿持有期间独立开关
- 机会腿的触发依据不再只看保护性压力，还可以看：
  - 短线反向 edge
  - 盘口冲击后的回归机会
  - 信号分歧加大但主方向未失效
  - 波动率异常放大后的短时均值回归

约束：

- 机会腿默认不能超过主腿的某个比例
- 机会腿必须有自己的最小持有时间与再平衡冷却
- 机会腿的胜率、费耗和 churn 必须单独记账

### 5.2 Independent

`independent` 的定义是：

- long book 和 short book 各自都有独立的信号、状态、冷却、持有期和试盘守护
- 任何一条腿都可以在另一条腿存在时独立开平
- 任何一条腿都可以在另一条腿为零时独立存在
- `long` 与 `short` 不再被定义成“主腿/副腿”，而是两本自治仓位簿

约束：

- 仍然必须受总毛敞口、净敞口、单腿敞口三层风控约束
- operator 必须能区分“方向主腿”和“独立机会腿”的来源
- 对账和恢复必须继续按腿工作，且要能识别腿来源与策略归因

## 6. 为什么必须单开新任务

必须单独立项，原因有 5 个：

1. `protective` 的决策器是防守型启发式评分，不适合作为机会型交易引擎直接复用。
2. `independent` 不是多几个参数，而是策略状态机和试盘守护口径的升级。
3. `opportunistic` 与 `independent` 都会增加成交次数、费耗和账本复杂度，风险面明显大于 `protective`。
4. operator、审计、回放和恢复页面必须新增“腿来源”和“腿模式”诊断，否则运维无法分辨当前双腿是保护、机会还是独立书。
5. 如果把这两种模式直接混进 `protective` 交付，会把当前已经稳定的 Phase 7 保护性能力重新搅乱。

## 7. 配置设计

在现有配置基础上，新增以下建议配置项：

```yaml
strategy_hedge_overlay_mode: protective          # protective | opportunistic | independent

strategy_hedge_opportunistic_enabled: false
strategy_hedge_opportunistic_open_threshold: 0.62
strategy_hedge_opportunistic_close_threshold: 0.46
strategy_hedge_opportunistic_max_ratio: 0.35
strategy_hedge_opportunistic_min_hold_seconds: 180
strategy_hedge_opportunistic_rebalance_cooldown_seconds: 90
strategy_hedge_opportunistic_max_fee_drag_ratio: 0.18
strategy_hedge_opportunistic_max_churn_ratio: 0.22

strategy_hedge_independent_enabled: false
strategy_hedge_independent_long_entry_threshold: 0.66
strategy_hedge_independent_short_entry_threshold: 0.66
strategy_hedge_independent_long_scale_in_threshold: 0.70
strategy_hedge_independent_short_scale_in_threshold: 0.70
strategy_hedge_independent_long_min_hold_seconds: 300
strategy_hedge_independent_short_min_hold_seconds: 300
strategy_hedge_independent_rebalance_cooldown_seconds: 120
strategy_hedge_independent_trial_guard_enabled: true
```

约束：

- `opportunistic` 和 `independent` 都必须默认关闭
- 只有 `derivatives + hedge` 运行线允许启用
- `protective / opportunistic / independent` 需要明确是互斥模式，还是允许按更高层策略切换；本任务建议第一版保持互斥

## 8. 领域模型增量

### 8.1 Overlay 决策对象

当前已有 `HedgeOverlayDecision`，但还偏向 `protective`。本任务需要扩展成通用对象，至少包含：

- `configured_mode`
- `effective_mode`
- `active`
- `state`
- `main_leg_signal`
- `main_leg_current_qty`
- `main_leg_target_qty`
- `overlay_leg_signal`
- `overlay_leg_current_qty`
- `overlay_leg_target_qty`
- `overlay_source`
- `reason_codes`
- `blocked_reasons`
- `min_hold_remaining_seconds`
- `rebalance_cooldown_remaining_seconds`
- `fee_drag_ratio`
- `churn_ratio`

### 8.2 腿来源归因

需要给 `StrategyLegIntent`、订单审计和对账摘要增加来源字段：

- `protective`
- `opportunistic`
- `independent_long_book`
- `independent_short_book`

否则后续 operator 无法回答：

- 这条 short 腿是保护腿还是独立空书？
- 这条 long 腿是主腿还是机会腿？
- 为什么当前允许双边同时扩张？

## 9. 实施拆解

本任务建议拆成 4 个子阶段，而不是一次性放开：

### 9.1 Phase A：Opportunistic 决策与配置

范围：

- `aats/bootstrap/settings.py`
- `aats/services/decision_engine/target_position.py`
- `aats/schemas/decision.py`
- `aats/schemas/strategy_runtime.py`

目标：

- 新增 opportunistic 参数
- 让 directional 在 `overlay_mode=opportunistic` 时能生成机会腿
- 机会腿和保护腿不能共用同一套 reason code

验收：

- 单测能覆盖开启、关闭、最小持有、冷却、费耗拦截
- runtime 能看到 `effective_mode=opportunistic`

### 9.2 Phase B：Independent 双书状态机

范围：

- `aats/services/decision_engine/context_builder.py`
- `aats/services/decision_engine/target_position.py`
- `aats/services/governance_engine/trial_guard.py`
- `aats/services/governance_engine/risk.py`

目标：

- long book / short book 各自有状态
- 低边际 streak、trial guard、冷却不再跨腿串扰
- 同 symbol 下允许两本书独立持有和调整

验收：

- long 连亏不会把 short 一起关掉
- short 冷却不会阻断 long 的合法再进场

### 9.3 Phase C：Operator / 审计 / 回放

范围：

- `aats/services/operator/query_service.py`
- `aats/services/operator/audit_replay_queries.py`
- `aats/api/static/modules/views/strategy-view.js`
- `aats/api/static/modules/detail-drawers.js`

目标：

- UI 能区分 protective / opportunistic / independent
- 审计页能展示腿来源、腿模式、腿级试盘守护结果
- 回放结果能区分是保护收益还是机会收益

验收：

- `/strategy/runtime`、`/audit/{decision_id}`、策略页都能看到 overlay 来源和模式
- 风险与恢复页对双书状态没有误导性文案

### 9.4 Phase D：灰度上线与回放样本

范围：

- managed profiles
- 运行手册
- 回放脚本和报告

目标：

- 先开放 `opportunistic`
- `independent` 先只做回放与 dry-run
- 实盘放开前必须给出样本回放报告和 operator 手册

验收：

- 至少 2 组历史回放样本
- 至少 1 组 dry-run 观察样本
- 有明确开关和回滚手册

## 10. 核心风险

### 10.1 Opportunistic 风险

- 成交次数上升，费耗与 churn 明显增加
- 机会腿与保护腿容易混淆
- 如果没有独立的 fee/churn 限制，会把主腿收益吃掉

### 10.2 Independent 风险

- 双书自治后，账本和 operator 复杂度显著提升
- trial guard 和 cooldown 如果仍保留 symbol 聚合口径，会直接误伤另一条腿
- recovery 与 rebaseline 如果不记录腿来源，会导致恢复语义不清

### 10.3 上线风险

- 不能在 `protective` 刚稳定后立刻把两种模式一起实盘打开
- 不能跳过 operator 审计改造直接放开真实下单
- 不能没有回放样本就上 `independent`

## 11. 验收标准

本任务完成的最低标准是：

1. `opportunistic` 有明确配置、决策对象、UI、审计和回放样本
2. `independent` 至少完成 dry-run / replay 级别能力，不得只存在配置枚举
3. long / short 的 trial guard、cooldown、fee/churn attribution 不再互相污染
4. `/strategy/runtime` 和 operator 审计能明确显示腿来源与 overlay 模式
5. 可以明确回答任意一条腿：
   - 它为什么被打开
   - 它属于哪种 overlay
   - 它为什么还没被关闭
   - 它当前受哪些限制

## 12. 回滚策略

回滚顺序必须清晰：

1. 先关闭 `strategy_hedge_opportunistic_enabled`
2. 再关闭 `strategy_hedge_independent_enabled`
3. 保留 `protective` 作为最后兜底模式
4. 如需彻底回退，再退回 `strategy_hedge_overlay_mode=protective`

不允许直接删除字段或复用旧 net mode 语义掩盖问题。

## 13. 建议的下一步

建议以本任务书为基线，先单独开工 `Phase A：Opportunistic 决策与配置`，不要一上来就做 `independent` 实盘。

原因很简单：

- `opportunistic` 可以建立在现有 `protective` 和显式腿执行链之上
- `independent` 会显著扩大状态机、试盘守护和 operator 的复杂度
- 先做 `opportunistic`，能更快验证“机会腿”这一抽象是否稳固

一句话建议：

**下一步先做 opportunistic，independent 作为随后阶段推进，而不是同一轮同时落地。**
