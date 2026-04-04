# Task 90: 合约对冲模式彻底改造任务书

## 1. 业务目标与边界

### 1.1 目标

把当前合约运行模式从“单一净仓位控制（net exposure control）”升级成“可用的双腿对冲模式（hedge mode / long-short books）”，满足以下真实交易需求：

- 同一合约同一时刻允许同时持有 `long` 与 `short` 两条腿
- 策略可以在保留原方向仓位的前提下，直接开立相反方向订单做保护性对冲或机会型对冲
- 对账、恢复、风控、PnL、冷却、试盘守护、控制面展示都按“腿”而不是只按“净仓位”工作
- 与 OKX 的 `long_short_mode` 账户语义严格一致，不靠本地隐式猜测

### 1.2 非目标

本次不是以下事项：

- 不是只做“反手更快”的参数微调
- 不是只做执行器层的 `posSide` 补丁
- 不是仅支持“先平再开”的过渡式 close-then-open 语义作为最终形态
- 不是给现货模式引入同样的双腿账本

### 1.3 目标最终形态

本次改造的最终目标不是 `close_then_open`，而是：

- 合约运行域支持 `hedge mode`
- 策略与执行以双腿账本为主语义
- 允许“已有空腿时直接开多腿”“已有多腿时直接开空腿”
- `net_qty` 退化为派生指标，不能再作为主决策状态

`close_then_open` 只能作为迁移阶段的上线策略，不是最终系统能力定义。

## 2. 当前行为摘要

当前代码已经具备部分 hedge mode 接口痕迹，但上层主模型仍是 net mode：

- OKX 适配层已经支持 `position_mode`、`pos_side`、`reduce_only`、`close_only`
- `OKXExecutionAdapter` 已经会校验 `long_short_mode` 下 `posSide` 是否匹配
- `PortfolioState` 内部按 `position_key` 保存仓位，键值本身可区分 `pos_side`
- 但决策层 `DecisionContext` 只有一个 `current_position_qty`
- `PositionTarget` 只有一个 `target_position_qty`
- `DecisionContextBuilder._position_qty()` 会把同 symbol 仓位直接求和
- `ExecutionPlanner` 会把“当前空、目标多”解释成 `reverse_to_long`，而不是“保留空腿再开多腿”
- 风控仍以单一目标仓位、单一净名义、本地净仓位口径为主
- 对账和恢复也主要围绕 symbol 聚合后的净仓位和账户总余额工作

因此，当前系统并不真正支持 hedge mode，只是底层执行字段部分兼容交易所的 hedge mode 表达。

## 3. 核心问题定义

### 3.1 当前问题不是单点 bug，而是主模型错误

现在系统的根问题是：

- 账户接入层读取了交易所的双边信息
- 但决策、风控、恢复、对账、展示继续把它们净额化

这会导致：

- 本地策略看不到“多腿”和“空腿”两本书
- 相反方向新订单会被误解释为“减仓/反手”
- 同一 symbol 双边仓位会在控制面和对账层被求和
- 对账与恢复会把本来合法的双边持仓误判为异常

### 3.2 真实要支持的交易语义

系统要支持以下 3 类动作：

1. 主腿建仓
   - `open_long`
   - `open_short`

2. 腿内调整
   - `reduce_long`
   - `reduce_short`
   - `close_long`
   - `close_short`

3. 对冲腿操作
   - 在存在 `short` 的前提下直接 `open_long`
   - 在存在 `long` 的前提下直接 `open_short`

这里的“直接开相反订单”必须被建模成一条新的腿动作，不能再被隐式翻译成 signed flip。

## 4. 总体改造原则

### 4.1 先改账户与交易所接入层，再改策略层

这是本次改造的硬原则。

原因：

- hedge mode 首先是交易所账户/仓位模式
- 不是策略输出层的概念
- 如果账户实际仍处于 `net_mode`，上层再怎么支持双腿都没有意义

### 4.2 先把“语义正确”做出来，再做策略优化

必须先完成：

- 交易所模式校验
- 双腿仓位状态
- 双腿订单意图
- 双腿风控
- 双腿对账
- 双腿恢复

然后再做：

- protective hedge overlay
- opportunistic hedge
- independent books 策略自治

## 5. 配置与环境隔离设计

新增以下核心配置项：

```yaml
derivatives_position_mode: hedge                  # net | hedge
derivatives_hedge_transition_mode: independent_books
derivatives_require_exchange_pos_mode_match: true

risk_max_long_notional: 5000
risk_max_short_notional: 5000
risk_max_gross_notional: 7000
risk_max_net_notional: 2500

risk_max_long_contracts_per_symbol: 0            # 0 表示按 notional 限制
risk_max_short_contracts_per_symbol: 0
risk_max_gross_leverage: 0

strategy_hedge_overlay_enabled: true
strategy_hedge_overlay_mode: protective          # protective | opportunistic | independent
strategy_hedge_open_threshold: 0.58
strategy_hedge_close_threshold: 0.42
strategy_hedge_max_ratio: 0.50
strategy_hedge_min_hold_seconds: 300
strategy_hedge_rebalance_cooldown_seconds: 120
```

说明：

- `derivatives_position_mode` 是运行时显式开关，禁止通过交易所回包或仓位形态隐式推断
- `derivatives_require_exchange_pos_mode_match=true` 时，启动必须 fail fast
- `derivatives_hedge_transition_mode` 的最终目标建议默认 `independent_books`
- 可通过 managed profile 明确区分：
  - `derivatives_net`
  - `derivatives_hedge`

## 6. 模块职责与领域模型

### 6.1 账户与交易所接入层

涉及模块：

- `aats/services/execution_engine/okx_account.py`
- `aats/services/execution_engine/okx_adapter.py`
- `aats/services/execution_engine/okx_rest.py`
- `aats/services/execution_engine/okx_private_websocket.py`

职责改造：

- 启动时主动读取交易所 `posMode`
- 若配置要求 `hedge` 而交易所不是 `long_short_mode`，直接阻止启动
- 所有合约订单都必须显式携带：
  - `side`
  - `pos_side`
  - `position_mode`
  - `td_mode`

### 6.2 仓位状态层

涉及模块：

- `aats/services/portfolio_service/positions.py`
- `aats/services/portfolio_service/snapshots.py`
- `aats/services/decision_engine/context_builder.py`
- `aats/services/operator/query_service.py`

现状问题：

- `PortfolioState` 虽按 `position_key` 存储，但上层消费经常按 symbol 重新聚合求和
- `DecisionContextBuilder._position_qty()` 直接返回净仓位
- UI 聚合逻辑会把 long/short 腿合成一条 `position_qty`

目标模型：

```python
class PositionLegState:
    symbol: str
    pos_side: Literal["long", "short"]
    quantity: Decimal
    avg_entry_price: Decimal
    unrealized_pnl: Decimal
    margin_mode: str
    position_mode: str
    leverage: float
    margin_allocated: Decimal | None
    maintenance_margin: Decimal | None
    margin_ratio: Decimal | None
    liquidation_price: Decimal | None

class InstrumentPositionState:
    symbol: str
    long_leg: PositionLegState | None
    short_leg: PositionLegState | None
    net_qty: Decimal
    gross_qty: Decimal
    net_notional: Decimal
    gross_notional: Decimal
```

约束：

- `net_qty` 只能是派生值
- 所有决策逻辑禁止以 `net_qty` 作为唯一主状态

### 6.3 目标仓位模型

涉及模块：

- `aats/schemas/decision.py`
- `aats/services/decision_engine/target_position.py`
- `aats/services/decision_engine/orchestrator.py`

现状：

- `PositionTarget` 只有一套 signed quantity

目标：

```python
class InstrumentTarget:
    symbol: str
    target_long_qty: Decimal
    target_short_qty: Decimal
    current_long_qty: Decimal
    current_short_qty: Decimal
    long_intent: Literal["open", "hold", "reduce", "close"]
    short_intent: Literal["open", "hold", "reduce", "close"]
    net_target_qty: Decimal
    gross_target_qty: Decimal
```

要求：

- `PositionTarget` 不得继续作为合约 hedge mode 主 schema
- 可以保留旧结构给现货和合约 net mode 使用
- 合约 hedge mode 必须切到新的双目标 schema

### 6.4 订单意图模型

涉及模块：

- `aats/schemas/execution.py`
- `aats/services/execution_engine/planner.py`
- `aats/services/execution_engine/order_manager.py`

目标模型：

```python
class LegOrderIntent:
    symbol: str
    product_type: Literal["derivatives"]
    side: Literal["buy", "sell"]
    pos_side: Literal["long", "short"]
    action: Literal["open", "reduce", "close"]
    quantity: Decimal
    reduce_only: bool
    position_mode: Literal["long_short_mode"]
    td_mode: Literal["cross", "isolated"]
```

强约束：

- 禁止执行器根据 signed net qty 自己猜订单意图
- 所有提交流程统一改成显式腿订单
- `submit_leg_order()` 成为合约下单主入口

### 6.5 风控层

涉及模块：

- `aats/services/governance_engine/risk.py`
- `aats/services/govenance_engine/derivatives_live_guard.py`
- `aats/services/govenance_engine/recovery_posture.py`

现状：

- 风控以净目标仓位、净目标杠杆、净名义、净保证金路径为主

目标：

至少拆成四口径：

- `long_notional`
- `short_notional`
- `gross_notional = long_notional + short_notional`
- `net_notional = abs(long_notional - short_notional)`

必要限制：

```text
long_notional  <= risk_max_long_notional
short_notional <= risk_max_short_notional
gross_notional <= risk_max_gross_notional
net_notional   <= risk_max_net_notional
```

此外新增：

- 单腿保证金占用
- 总毛杠杆
- 净敞口占比
- 多腿/空腿各自冷却与止损状态

### 6.6 对账、恢复与持仓同步

涉及模块：

- `aats/services/reconciliation_service/comparator.py`
- `aats/services/reconciliation_service/repair.py`
- `aats/services/execution_engine/recovery.py`
- `aats/services/runtime_scope.py`

现状问题：

- 对账与控制面常按 symbol 聚合净仓位
- 账户余额和仓位经常按账户总状态比较，而非按腿比较

目标：

对账层必须改成以下维度：

- `exchange_long_qty == local_long_qty`
- `exchange_short_qty == local_short_qty`
- `exchange_long_margin == local_long_margin`
- `exchange_short_margin == local_short_margin`

异常类型至少包括：

- only exchange long exists
- only exchange short exists
- local long exists but exchange missing
- local short exists but exchange missing
- both legs exist but one side qty mismatch
- long leg margin mismatch
- short leg margin mismatch
- exchange still in net_mode while runtime configured hedge

恢复层要求：

- 不能把双边合法持仓当成“未知仓位”
- 不能把另一侧腿误归类为 `without_local_execution_chain`
- 必须支持按腿恢复 open orders、fills、obligations

### 6.7 控制面与展示

涉及模块：

- `aats/services/operator/query_service.py`
- `aats/api/static/modules/views/*`

现状：

- 本地和交易所持仓会被聚合成单行净仓

目标：

同一 symbol 在 UI 至少展示：

- `long_leg`
- `short_leg`
- `net_qty`
- `gross_qty`
- `gross_notional`
- `net_notional`

风险页与恢复页也要展示：

- 当前运行模式是否为 `hedge`
- 交易所实际 `posMode`
- 是否存在单腿对账异常

## 7. 输入输出接口设计

### 7.1 外部接口

外部接口变更原则：

- 不静默更改现有 API 含义
- 对于现有只返回净仓位的接口，新增 hedge-aware 结构并做版本兼容

建议新增结构：

```json
{
  "symbol": "BTC-USDT-SWAP",
  "position_mode": "long_short_mode",
  "long_leg": {
    "qty": "1.0",
    "avg_entry_price": "68000"
  },
  "short_leg": {
    "qty": "0.5",
    "avg_entry_price": "70500"
  },
  "net_qty": "0.5",
  "gross_qty": "1.5"
}
```

### 7.2 内部接口

执行入口统一为：

```python
submit_leg_order(
    inst_id: str,
    td_mode: str,
    side: str,
    pos_side: str,
    qty: Decimal,
    reduce_only: bool = False,
    client_order_id: str | None = None,
)
```

策略与执行之间不得再直接传：

- `signed target qty`
- `buy/sell + 自动猜测是开还是平`

## 8. 数据库与持久化设计

### 8.1 需要调整的核心持久化对象

涉及表：

- `portfolio_snapshots`
- `order_states`
- `fill_events`
- `fill_outcomes`
- `execution_orders`
- `execution_fills`
- `reconciliation_reports`
- `strategy_runtime_*`

### 8.2 Schema 调整原则

已有 `position_mode` / `pos_side` 字段的表继续保留并升级为主语义字段。

新增或强化以下约束：

- 合约 hedge mode 下，`position_mode` 不允许为空
- `pos_side` 不允许为空
- `pos_side in {"long", "short"}`
- 同一个 `client_order_id` 不能跨 `pos_side` 复用

### 8.3 快照持久化

`portfolio_snapshots` 需要保证能同时持久化：

- `BTC-USDT-SWAP:LONG`
- `BTC-USDT-SWAP:SHORT`

不能在保存前就被 symbol 聚合成一条净仓记录。

### 8.4 索引建议

为以下组合建立或强化索引：

- `(symbol, product_type, margin_mode, position_mode, pos_side)`
- `(client_order_id, symbol, pos_side)`
- `(exchange_order_id, symbol, pos_side)`
- `(reconciliation_id, symbol, pos_side)`

## 9. 事务、一致性与并发

### 9.1 关键一致性要求

双腿改单必须满足：

- 同一腿的状态更新串行
- 一次“开多腿 + 平空腿”组合动作有统一关联 ID
- 不同腿允许并行，但同 symbol 下需要 bundle 级关联

### 9.2 幂等

以下对象必须具备幂等键：

- leg order intent
- execution order state
- fill ingest
- reconciliation mismatch finding

### 9.3 并发策略

建议引入：

- `strategy_bundle_id`
- `leg_id`
- `transition_group_id`

用于表达：

- 同一 hedge 操作下的一组腿动作
- 可追踪“先开多腿，再减空腿”或“先减空腿，再开多腿”的顺序

## 10. 认证、鉴权与数据安全

本次不新增新的鉴权模型，但需要新增写操作保护：

- 任何切换 `derivatives_position_mode` 的管理操作必须是 admin 权限
- 任何触发“净仓迁移到 hedge mode”的运维动作必须写审计日志
- 交易所账户实际 `posMode` 不匹配时，不允许 operator 在 UI 里直接点“忽略继续”

## 11. 错误处理与幂等策略

新增错误码至少包括：

- `okx_exchange_pos_mode_mismatch`
- `hedge_mode_requires_empty_derivatives_positions_before_switch`
- `leg_order_missing_pos_side`
- `leg_order_pos_side_action_conflict`
- `hedge_reconciliation_long_leg_missing`
- `hedge_reconciliation_short_leg_missing`
- `hedge_runtime_net_model_payload_disallowed`

错误处理原则：

- 账户模式不匹配：启动失败
- 订单语义缺失：下单失败
- 双腿对账不一致：进入 review-required，但不能误归为 signed net drift

## 12. 状态流转与生命周期

### 12.1 账户模式生命周期

```text
configured_net
configured_hedge_pending_exchange_match
hedge_ready
hedge_degraded
hedge_review_required
```

### 12.2 腿生命周期

每条腿分别维护：

```text
flat -> opening -> open -> reducing -> closing -> closed
```

### 12.3 组合生命周期

同 symbol 聚合状态：

```text
flat
long_only
short_only
hedged
transitioning
recovery_required
```

## 13. 缓存、性能与资源消耗

### 13.1 主要性能风险

- 对账从单条 symbol 净仓变成双腿比较，字段与 finding 数量会上升
- 控制面聚合会更重
- 风控计算由单一 notional 增加到 long/short/gross/net 四套指标

### 13.2 性能要求

- UI 聚合层可以展示 `net`/`gross` 派生值，但后端存储必须保留腿明细
- 对账比较优先按 `(symbol, pos_side)` 建立 map，避免 N²
- 运行时只缓存“当前账户腿状态”，不缓存推导后的 signed summary 作为真相

## 14. 日志、监控与审计

新增关键日志事件：

- `okx_exchange_position_mode_checked`
- `okx_exchange_position_mode_mismatch`
- `hedge_leg_order_submitted`
- `hedge_leg_order_rejected`
- `hedge_leg_reconciliation_mismatch_detected`
- `hedge_mode_transition_group_started`
- `hedge_mode_transition_group_completed`

监控指标新增：

- `hedge_long_notional`
- `hedge_short_notional`
- `hedge_gross_notional`
- `hedge_net_notional`
- `hedge_long_leg_count`
- `hedge_short_leg_count`
- `hedge_leg_reconciliation_mismatch_count`

审计要求：

- 任何 `position mode` 变更
- 任何 hedge transition policy 变更
- 任何 operator 手工关闭某一腿

都必须进入 operator audit trail。

## 15. 测试策略

### 15.1 单元测试

必须新增/重写以下测试族：

- OKX `posMode` 读取与 fail-fast
- `LegOrderIntent` 组合合法性
- long/short/gross/net 风控约束
- 双腿对账比较
- 双腿恢复与 only-reduce 归因
- 双腿冷却、PnL、low-edge streak 分离

### 15.2 集成测试

至少覆盖：

1. 账户为 `long_short_mode` 时成功启动
2. 配置要求 `hedge` 但交易所为 `net_mode` 时启动失败
3. 同 symbol 已有 short，直接 `open_long` 成功，不被解释为 reversal
4. 同 symbol 已有 long，直接 `open_short` 成功，不被解释为 reduce
5. 对账时 long 与 short 腿分别比对，不被净额化
6. 控制面能同时展示 long/short 两腿

### 15.3 回放测试

必须补一组历史回放样本：

- 单腿趋势行情
- 双腿保护性对冲
- 双腿并存后的减仓/平仓
- 交易所一侧腿缺失
- 恢复后本地与交易所存在 long/short 分离状态

## 16. 迁移、回滚与兼容性

### 16.1 迁移原则

迁移必须分层进行：

1. 先接入账户模式与 fail-fast
2. 再改双腿仓位模型
3. 再改订单意图
4. 再改风控
5. 再改对账与恢复
6. 最后改策略层 hedge overlay

### 16.2 向后兼容

- 现货与合约 net mode 仍保留旧路径
- hedge mode 走新路径
- 不允许在同一 runtime 同时混用新旧目标模型

### 16.3 回滚策略

回滚条件：

- 若双腿风控、对账、恢复任一未完成，不允许开启 `derivatives_position_mode=hedge`

回滚方式：

- 配置级回滚到 `net`
- 代码级保留 net/hedge 双实现
- 数据库保留新增字段与索引，不做破坏式删除

## 17. 策略层改造方案

### 17.1 最终目标

最终支持三种 overlay 模式：

- `protective`
- `opportunistic`
- `independent`

### 17.2 第一阶段上线目标

第一阶段推荐上线：

- 主 directional 信号继续决定主腿
- 对冲腿按保护性规则开启
- 允许同 symbol 双边持仓共存

### 17.3 策略层最少新增能力

必须新增：

- `main_leg_signal`
- `hedge_leg_signal`
- `hedge_ratio`
- `hedge_open_condition`
- `hedge_close_condition`

策略层必须停止输出：

- 单一 signed target 作为合约 hedge mode 主结果

## 18. 代码组织与依赖

建议新增或拆分目录：

- `aats/services/hedge_mode/`
  - `account_mode.py`
  - `leg_intents.py`
  - `leg_risk.py`
  - `leg_reconciliation.py`
  - `leg_recovery.py`
  - `leg_cooldowns.py`

保留原则：

- 不要把 hedge 逻辑继续散落在 net mode 文件里通过大量 `if product_type == "derivatives"` 临时拼接
- 合约 `net` 与 `hedge` 最终要有明确分支边界

## 19. 文档与运维手册

需要补齐以下运维文档：

- 如何把 OKX 账户切到 `long_short_mode`
- 切换前必须清空哪些仓位/委托
- 如何验证本地配置与交易所 `posMode` 一致
- 出现单腿对账异常时如何处理
- 如何观察 gross/net 风险指标

## 20. 部署顺序与验收标准

### 20.1 推荐部署顺序

1. 先上线只读能力：
   - 读取交易所 `posMode`
   - UI 展示双腿
2. 再上线 fail-fast：
   - 配置/交易所模式不一致则拒绝启动
3. 再上线双腿持仓状态与对账
4. 再上线双腿订单意图与执行
5. 再上线双腿风控
6. 最后上线 hedge overlay 策略

### 20.2 验收标准

必须全部满足才算可用：

- 配置为 `hedge` 时，交易所不在 `long_short_mode` 会直接拒绝启动
- 同一 symbol 可同时存在 long 与 short 两腿，本地状态不被净额化
- 下单时 `side + posSide + action` 语义明确，无隐式推断
- 风控同时限制 long、short、gross、net 四口径
- 对账不会把合法双腿持仓误判成异常 signed drift
- 恢复不会把双腿状态误归类为 unknown state
- 控制面与 operator API 能同时展示两条腿
- 试盘守护、冷却、PnL 归因至少做到按腿隔离

## 21. 分阶段实施计划

### 阶段 A：账户模式与 fail-fast

- 新增 `derivatives_position_mode`
- 启动时校验交易所 `posMode`
- 不匹配直接拒绝启动

### 阶段 B：双腿仓位状态

- `PortfolioState` 与 snapshot 主消费逻辑改为双腿
- `DecisionContext` 不再只暴露净仓

### 阶段 C：双腿订单语义

- 引入 `LegOrderIntent`
- 执行入口改为 `submit_leg_order`

### 阶段 D：双腿风控

- long / short / gross / net 四口径
- 保护性约束与保证金约束按腿工作

### 阶段 E：双腿对账与恢复

- reconciliation comparator 重写为腿级比较
- recovery 与 only-reduce 逻辑改为腿级归因

### 阶段 F：策略 overlay

- 先支持 `protective`
- 再支持 `opportunistic`
- 最后支持 `independent`

## 22. 关键架构决策

### 22.1 必须保留 net 派生字段

理由：

- 控制面、风控报表、运维观测仍然需要 `net` 视角

但必须明确：

- `net` 是派生字段，不是主状态

### 22.2 最终过渡模式建议

虽然迁移中可使用 `close_then_open` 作为安全阶段，但最终默认目标必须是：

```yaml
derivatives_hedge_transition_mode: independent_books
```

因为只有这样，才真正支持你要的“直接开相反订单对冲”。

## 23. 主要风险

### 23.1 最大工程风险

风控、对账、恢复若仍残留 net 逻辑，会把合法双腿仓位误判为异常。

### 23.2 最大交易风险

若只改执行器、不改风控与恢复，会导致：

- 交易所已合法双边持仓
- 本地系统仍把它当 signed net
- 然后进入错误 only-reduce / review-required / halt

### 23.3 最大迁移风险

若在账户仍有仓位时切换 OKX `posMode`，会失败或产生高风险运维事件。

## 24. 最终结论

这次不是参数优化任务，而是一次合约主模型重构。

只有完成以下 4 件事，系统才算真正支持 hedge mode：

1. 账户模式与交易所语义一致
2. 仓位与目标模型改成双腿
3. 执行与风控改成显式腿语义
4. 对账与恢复按腿工作

只做其中一部分，系统都仍然会表现为“看起来有 hedge mode 字段，但实际上不能可靠使用”。
