# Skill: 排查“系统一直不下单”的标准流程

## 适用对象
本技能适用于基于策略家族（directional / independent / protective / opportunistic / smart_arbitrage）、allocator、execution planner、order service、ledger / settlement / outbox / inbox 架构的自动交易系统。

---

# 0. 目标

当系统表现为：

- 一直不下单
- 没有生成 execution order
- 有信号但没有实际交易
- allocator / family 看起来在运行，但最终没有订单 / fill

本技能的目标是帮助 Codex **按固定顺序**定位到底卡在哪一层，而不是盲目翻所有表和日志。

---

# 1. 排查原则

不要一上来就看 `execution_orders` 或 `execution_fills`。  
必须按这个顺序查：

1. 决策链有没有跑
2. allocator 有没有批准 family
3. family 自己是不是 blocked
4. 有没有 execution bundle / execution plan
5. 有没有 execution command
6. 有没有 execution order
7. 有没有 venue reject / IOC no-fill

如果上游根本没生成 `position_target` 或 `allocation_decision`，继续看 execution 层没有意义。

---

# 2. 先判断是不是“看错库 / 没跑主循环 / 没持久化”

如果下面这些表是空的：

- `decision_audit_records`
- `portfolio_allocation_decisions`
- `strategy_sleeve_intents`

优先怀疑：

1. 你看的不是正在运行的数据库
2. orchestrator / scheduler 根本没跑
3. decision persistence / event persistence / projector 没启动
4. 当前 profile 其实是 shadow / diagnostics / dry-run

## 2.1 先执行这些 SQL

```sql
select count(*) as event_store_count, max(created_at) as last_event_at from event_store;
select count(*) as audit_count, max(updated_at) as last_audit_at from decision_audit_records;
select count(*) as allocation_count, max(created_at) as last_allocation_at from portfolio_allocation_decisions;
select count(*) as sleeve_intent_count, max(created_at) as last_sleeve_at from strategy_sleeve_intents;
select * from runtime_profile_activation order by activated_at desc limit 10;
select * from strategy_profile_activation_history order by created_at desc limit 10;
```

## 2.2 如何解释

### 情况 A：`event_store` 也是空的
说明系统大概率根本没往这套库写任何事件。  
优先检查：

- 数据库连接目标是不是对的
- orchestrator 是否运行
- event persistence 是否启用

### 情况 B：`event_store` 有数据，但其它表空
说明：

- 事件进库了
- 但 projector / projection worker 没跑
- 或者投影写表失败

### 情况 C：profile activation 记录为空
说明：

- 当前环境可能根本没走 runtime / strategy profile activation
- 或者你看的不是正在跑的库

---

# 3. 第一层：查一轮 decision 到底有没有走到 target / allocation / execution

## 3.1 优先查的表：`decision_audit_records`

### 为什么先看它
它能把一轮决策串起来，告诉你：

- 有没有 `position_target`
- 有没有 `portfolio_allocation_decision`
- 有没有 `execution_plan`
- 有没有 `strategy_execution_bundle`
- 有没有 `order intents / order states / fills`

### 查询 SQL
```sql
select
  audit_revision_id,
  decision_id,
  updated_at,
  decision_context_ref,
  position_target_ref,
  policy_decision_ref,
  risk_decision_ref,
  execution_plan_ref,
  order_intent_refs,
  order_state_refs,
  fill_event_refs,
  portfolio_delta_ref,
  reconciliation_refs,
  payload
from decision_audit_records
order by updated_at desc
limit 20;
```

### 重点字段
- `decision_id`
- `position_target_ref`
- `policy_decision_ref`
- `risk_decision_ref`
- `execution_plan_ref`
- `order_intent_refs`
- `order_state_refs`
- `fill_event_refs`
- `payload`

### 如何判断
#### 如果这些都空：
- `position_target_ref`
- `execution_plan_ref`
- `order_intent_refs`
- `order_state_refs`
- `fill_event_refs`

说明这一轮根本没走到可执行层。  
下一步去查：

- family
- allocator
- orchestrator
- persistence

#### 如果 `position_target_ref` 有，但 `execution_plan_ref` 没有
说明：

- 最终 target 有了
- 但没转成 execution plan

#### 如果 `execution_plan_ref` 有，但 `order_intent_refs` 为空
说明：

- plan 生成了
- 但没转成 order intent / order command

---

# 4. 第二层：查 allocator 最终批准了谁

## 4.1 优先查的表：`portfolio_allocation_decisions`

### 查询 SQL
```sql
select
  allocation_id,
  decision_id,
  symbol,
  product_type,
  margin_mode,
  automatic_enabled,
  route_action,
  primary_family,
  primary_strategy_sleeve_id,
  created_at,
  payload
from portfolio_allocation_decisions
where decision_id = '<decision_id>';
```

### 重点字段
- `allocation_id`
- `decision_id`
- `symbol`
- `route_action`
- `primary_family`
- `payload`

### `payload` 里重点看
- `selected_family`
- `approved_intents`
- `rejected_intents`
- `approved_families`
- `reason_codes`
- `blocked_reasons`
- `portfolio_requested_notional`
- `portfolio_approved_notional`

### 如何判断
#### 情况 A：`primary_family = directional`
说明：

- independent / overlay family 没拿到最终执行权
- 系统可能 fallback 到 directional

#### 情况 B：`route_action = hold_current` 或 `advisory_only`
说明：

- allocator 最终决定不执行交易

#### 情况 C：`approved_families = []`
说明：

- 没有任何 family 被批准
- 不下单是 allocator 的明确结果，不是 execution 问题

---

# 5. 第三层：查 family 自己是不是 blocked

这一步通常最关键，尤其当前 live owner 是 `independent` 时。

## 5.1 优先查的表：`strategy_sleeve_intents`

### 查询 SQL
```sql
select
  sleeve_intent_id,
  decision_id,
  family,
  strategy_sleeve_id,
  state,
  symbol,
  product_type,
  margin_mode,
  inventory_policy,
  route_action,
  allocation_id,
  automatic_enabled,
  budget_multiplier,
  allocator_weight,
  created_at,
  payload
from strategy_sleeve_intents
where decision_id = '<decision_id>'
order by family, created_at;
```

### 重点字段
- `family`
- `state`
- `route_action`
- `allocation_id`
- `payload`

### `payload` 里重点看
#### 对 `independent`
- `book_action`
- `blocked_reasons`
- `score`
- `entry_threshold`
- `close_threshold`
- `scale_in_threshold`
- `expected_signal_edge_bps`
- `expected_cost_bps`
- `expected_slippage_bps`
- `expected_net_edge_bps`
- `liquidity_quality_score`
- `score_stability_metrics`
- `execution_health`
- `close_reason`

#### 对 `directional`
- `family_action`
- `current_position_qty`
- `target_position_qty`
- `delta_position_qty`
- `expected_signal_edge_bps`
- `expected_cost_bps`
- `expected_net_edge_bps`

### 如何判断
#### independent `state = blocked`
说明不是 execution 层的问题，而是 independent 自己挡住了自己。  
最常见原因：

- `expected_cost_above_max_acceptable`
- `expected_net_edge_below_safe_threshold`
- `liquidity_quality_too_low`
- `score_stability_failed`
- `execution_health_degraded`
- `cooldown`
- `trial_guard`
- `fee_drag_guard`

#### directional `target_position_qty = 0` 且 `delta_position_qty = 0`
说明 directional 这轮本身就没有可执行仓位变化。  
如果 allocator fallback 到 directional，它也不会下单。

---

# 6. 第四层：查是否形成 execution bundle / execution plan

## 6.1 优先查的表：`strategy_execution_bundles`

### 查询 SQL
```sql
select
  strategy_bundle_id,
  decision_id,
  allocation_id,
  family,
  route_action,
  status,
  created_at,
  payload
from strategy_execution_bundles
where decision_id = '<decision_id>';
```

### 重点字段
- `strategy_bundle_id`
- `family`
- `route_action`
- `status`
- `payload`

### 如何判断
#### 没有 bundle
说明：

- 决策没有转成执行包
- 问题仍然在 coordinator / allocator / apply 层

#### 有 bundle，但 `status` 不推进
说明：

- 执行层被调用了
- 但 execution command / order 生成没继续走

---

# 7. 第五层：查 execution command 有没有生成并真正 enqueue

## 7.1 优先查的表：`execution_commands`

### 查询 SQL
```sql
select
  command_id,
  order_id,
  command_type,
  idempotency_key,
  state,
  attempt_count,
  last_error,
  command_payload,
  created_at,
  updated_at
from execution_commands
where order_id in (
  select order_id from execution_orders where decision_id = '<decision_id>'
)
order by created_at desc;
```

### 重点字段
- `order_id`
- `command_type`
- `idempotency_key`
- `state`
- `attempt_count`
- `last_error`
- `command_payload`

### 如何判断
#### 没有 command
说明：
- execution bundle / execution plan 没转成 command

#### 有 command，但 `state` 卡住
说明：
- enqueue / submit 层有问题

#### `attempt_count` 很高或 `last_error` 非空
看具体 submit 错误。

#### 同一 `idempotency_key` 一直命中旧命令
说明命令层被幂等吸收掉了。

---

# 8. 第六层：查是否真的建了订单

## 8.1 优先查的表：`execution_orders`

### 查询 SQL
```sql
select
  order_id,
  intent_id,
  decision_id,
  execution_attempt_id,
  client_order_id,
  venue_order_id,
  symbol,
  side,
  order_type,
  time_in_force,
  requested_qty,
  limit_price,
  reduce_only,
  close_only,
  td_mode,
  position_mode,
  pos_side,
  strategy_family,
  strategy_bundle_id,
  strategy_leg_role,
  created_at
from execution_orders
where decision_id = '<decision_id>'
order by created_at desc;
```

### 重点字段
- `order_id`
- `venue_order_id`
- `requested_qty`
- `order_type`
- `time_in_force`
- `td_mode`
- `position_mode`
- `pos_side`
- `strategy_family`
- `strategy_leg_role`

### 如何判断
#### 没有 order
说明：
- command 没有真正转成 order

#### 有 order，但 `venue_order_id` 为空
说明：
- 本地建单了
- 但没发到交易所，或发单失败

#### `requested_qty = 0` 或极小
说明：
- rounding / 最小下单量 / 最小名义价值把单吃掉了

---

# 9. 第七层：查是不是其实下单了，但没成交 / 被拒单

## 9.1 优先查的表：`execution_fills`

### 查询 SQL
```sql
select
  fill_id,
  venue_fill_id,
  order_id,
  execution_attempt_id,
  venue_order_id,
  client_order_id,
  decision_id,
  intent_id,
  symbol,
  side,
  fill_qty,
  fill_price,
  fee_amount,
  fee_currency,
  strategy_family,
  strategy_bundle_id,
  strategy_leg_role,
  liquidity_role,
  exchange_ts,
  ingestion_ts,
  raw_payload
from execution_fills
where decision_id = '<decision_id>'
order by ingestion_ts desc;
```

### 如何判断
#### 有 order 没有 fill
那不是“没下单”，而是：
- 被 reject
- 被 cancel
- IOC 直接空掉
- order expired

此时要去日志里查 venue adapter 的 submit / reject / cancel / expire。

---

# 10. 最常见的不下单原因及其对应定位点

## 10.1 independent 自己 blocked
### 症状
- `strategy_sleeve_intents.family = independent`
- `state = blocked`
- `route_action = advisory_only` / `hold_current`
- `payload.blocked_reasons` 非空

### 最常见原因
- `expected_cost_above_max_acceptable`
- `expected_net_edge_below_safe_threshold`
- `liquidity_quality_score` 太低
- `score_stability_metrics` 失败
- `execution_health` degraded
- cooldown / trial guard / churn guard / fee drag guard

---

## 10.2 allocator 没批准任何 family
### 症状
- `portfolio_allocation_decisions.route_action = advisory_only`
- `approved_families = []`
- `portfolio_approved_notional = 0`

### 解释
这不是 execution 故障，而是 allocator 明确决定本轮不交易。

---

## 10.3 independent 没拿到执行权，fallback 到 directional，但 directional 也没 delta
### 症状
- `portfolio_allocation_decisions.primary_family = directional`
- `reason_codes` 里有：
  - `independent_family_candidate_inactive`
  - `legacy_configured_strategy_directional_fallback`
- `strategy_sleeve_intents.family = directional`
  - `target_position_qty = 0`
  - `delta_position_qty = 0`

### 解释
系统确实 fallback 了，但 directional 本轮也没有可执行仓位变化。

---

## 10.4 qty 被 rounding / min size / min notional 吃掉
### 症状
- family / allocator 看起来都通过了
- `execution_orders.requested_qty = 0` 或非常小
- 或压根没生成 order，因为 planner 判定不可下单

### 重点查
- `execution_orders.requested_qty`
- final target / leg payload 中的 qty/notional
- venue symbol rules / precision / min qty / min notional

---

## 10.5 订单已建，但 venue submit 失败
### 症状
- `execution_orders` 有记录
- `venue_order_id` 为空
- `execution_commands.last_error` 非空
- `execution_fills` 为空

### 重点查
- `execution_commands.last_error`
- adapter 日志
- venue reject / submit failure

---

# 11. 必查日志关键词

如果数据库里能看到 `decision_id`，按这个 `decision_id` 搜日志。

## 11.1 决策层日志关键词
- `DecisionOrchestrator`
- `run_cycle`
- `decision_id`
- `strategy_coordinator`
- `allocation_decision`
- `selected_family`
- `primary_family`
- `blocked_reasons`
- `route_action`
- `family_action`

## 11.2 execution 层日志关键词
- `execution_plan`
- `strategy_execution_bundle`
- `enqueue_command`
- `idempotency_key`
- `submit`
- `venue_order_id`
- `reject`
- `cancel`
- `expire`
- `IOC`
- `min_qty`
- `min_notional`
- `precision`

## 11.3 persistence / projection 层日志关键词
- `event_store`
- `outbox`
- `projector`
- `projection`
- `persist`
- `commit`
- `sqlalchemy`
- `psycopg`

---

# 12. 结合当前系统最可能的排查路径

如果你当前系统表现为“一直不下单”，优先按下面顺序查：

## Step 1
先查 `decision_audit_records`

判断：
- 这轮有没有走到 target / allocation / execution

## Step 2
查 `portfolio_allocation_decisions`

判断：
- allocator 最终批准了谁
- 是不是 `advisory_only`
- 是不是 fallback 到 directional

## Step 3
查 `strategy_sleeve_intents`

判断：
- independent 到底是 blocked、hold、open、scale、de-risk
- 还是 directional 自己 delta=0

## Step 4
如果前面都通过，再看 `strategy_execution_bundles`

判断：
- 有没有形成执行包

## Step 5
再看 `execution_commands`

判断：
- 有没有 enqueue / submit
- 有没有幂等吸收 / last_error

## Step 6
再看 `execution_orders` 和 `execution_fills`

判断：
- 是真的没建单
- 还是建单了但被 reject / IOC 空掉

---

# 13. 给 Codex 的输出要求

当 Codex 用本技能排查时，不要只说“系统没下单”。  
必须输出：

1. **卡住层级**
   - strategy blocked / allocator hold / no execution plan / no command / no order / no fill

2. **证据**
   - 哪个表
   - 哪个字段
   - 哪个值

3. **最直接原因**
   - 例如：
     - `independent_short_book_expected_cost_above_max_acceptable`
     - `approved_families = []`
     - `target_position_qty = 0`
     - `requested_qty = 0`
     - `execution_commands.last_error = ...`

4. **下一步应该看什么**
   - 具体到文件 / 函数 / SQL / 日志关键词

---

# 14. 最后一句原则

**不要把“不下单”直接归因给 execution。**  
在大多数情况下，系统不下单的真正原因发生在更上游：

- family blocked
- allocator hold
- fallback family 也没有 delta
- risk / policy 不批准
- 或 qty 被最小交易单位吃掉

必须先用 `decision_audit_records`、`portfolio_allocation_decisions`、`strategy_sleeve_intents` 把决策链查清，再往 execution 层走。
