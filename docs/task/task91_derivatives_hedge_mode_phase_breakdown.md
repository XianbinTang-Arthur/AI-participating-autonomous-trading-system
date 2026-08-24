# Task 91: 合约对冲模式阶段拆解实施单

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 文档目的

本文是 [`Task 90`](task90_derivatives_hedge_mode_rearchitecture.md) 的施工拆解版，用于把“合约 hedge mode 彻底改造”落成可执行的阶段计划。

这份文档回答 6 个问题：

- 先做什么，后做什么
- 每个阶段的边界在哪里
- 每个阶段涉及哪些模块
- 每个阶段的交付物是什么
- 每个阶段的测试与验收是什么
- 哪些红线没有完成前不能进入下一阶段

## 2. 当前状态一句话总结

当前系统底层执行适配已经部分兼容 OKX `long_short_mode` 的字段表达，但上层决策、风控、对账、恢复、UI 仍然以“单一净仓位”作为主模型，因此现在不能可靠支持“同一合约同时持有 long 与 short 两条腿并直接开相反订单对冲”。

## 3. 实施总原则

### 3.1 先账户，后模型，最后策略

阶段顺序固定为：

1. 账户模式与交易所语义对齐
2. 仓位/订单/风控/对账主模型改成双腿
3. 最后再放策略 overlay

### 3.2 每阶段都要可停、可验、可回滚

每个阶段都必须满足：

- 有明确交付物
- 有最小测试集
- 有阶段性验收口径
- 有不进入下一阶段的红线

### 3.3 不接受“只改一半”

以下搭配禁止上线：

- 只改执行器，不改风控
- 只改持仓展示，不改对账
- 只改下单 `posSide`，不改恢复
- 只改策略，不改账户模式校验

## 4. 阶段总览

| 阶段 | 名称 | 目标 | 是否可单独上线 |
| --- | --- | --- | --- |
| Phase 0 | 预检与护栏 | 先把账户模式、配置、迁移前提校验补齐 | 是 |
| Phase 1 | 账户模式接入 | 让系统明确认识 `net` / `hedge`，并在启动时 fail fast | 是 |
| Phase 2 | 双腿仓位模型 | 把本地仓位与快照主模型从净仓位升级成双腿 | 否，仅内部能力 |
| Phase 3 | 腿级订单语义 | 把执行入口从 signed target 改成显式腿订单 | 否，仅内部能力 |
| Phase 4 | 腿级风控 | 把风控改成 long / short / gross / net 四口径 | 否，必须和 Phase 3 一起验收 |
| Phase 5 | 腿级对账与恢复 | 让 reconciliation / recovery 真正理解双腿 | 否，必须和 Phase 2-4 一起验收 |
| Phase 6 | 控制面与运维 | 让 operator/UI/监控/审计完整展示双腿系统 | 是，建立在 1-5 完成之上 |
| Phase 7 | 策略 overlay | 最后接入 protective / opportunistic / independent | 是，建立在 1-6 完成之上 |

## 5. Phase 0：预检与护栏

### 5.1 目标

在正式改造前，先补足所有会影响迁移安全的“硬护栏”，防止后续一边开发一边出现运行语义混乱。

### 5.2 范围

- `aats/bootstrap/settings.py`
- `aats/bootstrap/config.py`
- `aats/services/execution_engine/okx_account.py`
- `aats/services/operator/query_service.py`
- `configs/strategy_profiles/derivatives*.yaml`
- `.env.derivatives*`

### 5.3 工作项

1. 新增显式配置项：
   - `derivatives_position_mode`
   - `derivatives_hedge_transition_mode`
   - `derivatives_require_exchange_pos_mode_match`
2. 在系统 runtime summary 和 operator 页暴露：
   - 当前配置模式
   - 交易所实际 `posMode`
   - 是否匹配
3. 明确禁止：
   - `trading_product_type != derivatives` 时设置 hedge mode
   - `margin_mode == cash` 时设置 hedge mode
4. 增加迁移前检查：
   - 是否存在未完成委托
   - 是否存在持仓
   - 是否存在未完成恢复/对账异常

### 5.4 交付物

- 新配置项
- 启动前预检结果
- operator 只读展示

### 5.5 测试

- 配置校验单测
- runtime summary 单测
- operator API 最窄集成测试

### 5.6 阶段红线

以下任一未完成，不得进入 Phase 1：

- 运行时没有显式 `derivatives_position_mode`
- 交易所实际 `posMode` 无法观测
- operator 无法看到“配置模式 vs 交易所模式”的差异

## 6. Phase 1：账户模式接入与 fail-fast

### 6.1 目标

让系统在合约启动时真正把 OKX `posMode` 当成运行前提，而不是运行中顺手读取的一个状态字段。

### 6.2 范围

- `aats/services/execution_engine/okx_account.py`
- `aats/services/execution_engine/okx_adapter.py`
- `aats/bootstrap/config.py`
- `aats/api/routes.py`

### 6.3 工作项

1. 启动时读取交易所 `posMode`
2. `derivatives_position_mode=hedge` 且交易所不是 `long_short_mode` 时：
   - 启动失败
   - 错误码固定
   - operator 页面显示清晰原因
3. `derivatives_position_mode=net` 且交易所是 `long_short_mode` 时：
   - 也应失败
   - 防止系统用旧 net 语义运行到 hedge 账户上
4. 增加运维指引：
   - 切换前需无持仓
   - 切换前需无 open orders

### 6.4 交付物

- fail-fast 启动校验
- 明确错误码与日志
- operator 页面上的模式不匹配诊断

### 6.5 测试

- `posMode` 匹配时启动成功
- `posMode` 不匹配时启动失败
- API/控制面能看到不匹配状态

### 6.6 阶段红线

以下任一未完成，不得进入 Phase 2：

- net/hedge 模式启动时仍可静默混跑
- `posMode` 不匹配不会阻止启动
- 订单提交流程仍可在未知 `posMode` 下运行

## 7. Phase 2：双腿仓位状态与快照

### 7.1 目标

把本地仓位真相从“按 symbol 聚合后的 signed qty”改成“按 `symbol + pos_side` 管理的双腿仓位”。

### 7.2 范围

- `aats/services/portfolio_service/positions.py`
- `aats/services/portfolio_service/snapshots.py`
- `aats/services/decision_engine/context_builder.py`
- `aats/services/runtime_scope.py`
- `aats/schemas/portfolio.py`
- `aats/schemas/decision.py`
- `aats/services/operator/query_service.py`

### 7.3 工作项

1. 明确引入腿级状态对象：
   - `PositionLegState`
   - `InstrumentPositionState`
2. `PortfolioState` 的主消费路径改成按腿工作
3. `snapshot` 存储与读取必须保留：
   - `BTC-USDT-SWAP:LONG`
   - `BTC-USDT-SWAP:SHORT`
4. `DecisionContext` 不再只暴露 `current_position_qty`
5. 新增派生字段：
   - `net_qty`
   - `gross_qty`
   - `net_notional`
   - `gross_notional`
6. 停止在上层 helper 中把同 symbol legs 先求和再参与决策

### 7.4 交付物

- 双腿仓位状态对象
- 双腿快照读写链路
- hedge-aware decision context

### 7.5 测试

- 仓位加载单测
- snapshot builder 单测
- context builder 单测
- operator 查询层单测

### 7.6 阶段红线

以下任一存在，不得进入 Phase 3：

- `DecisionContext` 仍以净仓位为唯一主状态
- snapshot 保存前就被 symbol 聚合
- UI/query 层只有净仓，没有腿明细

## 8. Phase 3：腿级订单语义与执行入口

### 8.1 目标

把执行层从“signed target + buy/sell 推断意图”升级成“显式腿订单意图”。

### 8.2 范围

- `aats/schemas/execution.py`
- `aats/services/execution_engine/planner.py`
- `aats/services/execution_engine/order_manager.py`
- `aats/services/execution_engine/okx_adapter.py`
- `aats/services/execution_engine/paper_adapter.py`

### 8.3 工作项

1. 新增 `LegOrderIntent`
2. 明确动作集合：
   - `open`
   - `reduce`
   - `close`
3. 对合约 hedge mode 禁止继续从 signed target 自动推导：
   - `reverse_to_long`
   - `reverse_to_short`
4. 新增统一执行入口：
   - `submit_leg_order()`
5. 所有合约 hedge mode 提交必须显式包含：
   - `side`
   - `pos_side`
   - `action`
   - `position_mode`

### 8.4 交付物

- 新订单意图 schema
- planner 新分支
- adapter 新提交入口

### 8.5 测试

- `LegOrderIntent` 合法性单测
- planner 对 long/short/open/reduce/close 的映射单测
- adapter 在 `long_short_mode` 下的语义校验单测

### 8.6 阶段红线

以下任一未解决，不得进入 Phase 4：

- 执行器仍会根据 signed qty 猜测开平方向
- hedge mode 下仍可提交缺少 `pos_side` 的订单
- planner 仍把“直接开相反腿”解释成 reversal

## 9. Phase 4：腿级风控

### 9.1 目标

让风控从净头寸保护升级成：

- 单腿保护
- 毛敞口保护
- 净敞口保护

三层并行。

### 9.2 范围

- `aats/services/governance_engine/risk.py`
- `aats/services/governance_engine/derivatives_live_guard.py`
- `aats/schemas/governance.py`

### 9.3 工作项

1. 新增 long/short/gross/net 四口径指标
2. 新增四套限额：
   - `risk_max_long_notional`
   - `risk_max_short_notional`
   - `risk_max_gross_notional`
   - `risk_max_net_notional`
3. 把 `only_reduce_required` 改造成腿级约束：
   - 只限制某一腿继续扩张
   - 不错误阻断另一腿的合法减仓或保护性对冲
4. 保证金与杠杆计算增加：
   - 单腿保证金
   - 总毛杠杆
   - 净杠杆参考

### 9.4 交付物

- 新风控 schema
- 新限额配置
- hedge mode 风控评估分支

### 9.5 测试

- 四口径限额单测
- `only_reduce_required` 腿级行为单测
- live guard / recovery posture 相关回归测试

### 9.6 阶段红线

以下任一存在，不得进入 Phase 5：

- 风控仍只看净名义，不看毛敞口
- `long 100k / short 90k` 仍被当成“只剩 10k 风险”
- only-reduce 仍会把合法对冲腿也一起封死

## 10. Phase 5：腿级对账、恢复与同步

### 10.1 目标

让 reconciliation / recovery 真正理解双腿，不再把合法双边仓位误判成未知状态或 signed drift。

### 10.2 范围

- `aats/services/reconciliation_service/comparator.py`
- `aats/services/reconciliation_service/repair.py`
- `aats/services/execution_engine/recovery.py`
- `aats/services/governance_engine/recovery_posture.py`
- `aats/services/runtime_scope.py`

### 10.3 工作项

1. 对账从 symbol 净额比较改成腿级比较：
   - `exchange_long_qty == local_long_qty`
   - `exchange_short_qty == local_short_qty`
2. 对账 finding 分类改成腿级异常
3. 恢复逻辑支持：
   - long leg 恢复
   - short leg 恢复
   - dual-leg bundle 恢复
4. 修正“未知仓位链”检测：
   - 不能把合法另一腿误归为 `without_local_execution_chain`
5. 控制面恢复页显示腿级 mismatch

### 10.4 交付物

- 新 comparator 规则
- 新 recovery 归因
- 新 operator recovery 视图

### 10.5 测试

- 腿级 reconciliation 单测
- 腿级 recovery 单测
- 最窄 integration：recovery/operator 页面

### 10.6 阶段红线

以下任一存在，不得进入 Phase 6：

- 合法 long+short 并存仍会被识别成 unknown state
- 恢复链仍会把一条腿误判成异常外部仓位
- operator recovery 无法区分 long leg / short leg 异常

## 11. Phase 6：控制面、监控与运维

### 11.1 目标

让 operator、监控、审计、运维流程完整理解 hedge mode，而不是只有底层代码支持。

### 11.2 范围

- `aats/services/operator/query_service.py`
- `aats/api/static/modules/views/*`
- `aats/services/blocker_control/*`
- operator audit 相关模块

### 11.3 工作项

1. 仓位页显示：
   - long leg
   - short leg
   - net/gross 派生值
2. 风险页显示：
   - long notional
   - short notional
   - gross notional
   - net notional
3. 恢复页显示：
   - hedge mode
   - exchange posMode
   - leg mismatch
4. 审计日志增加：
   - mode 变更
   - leg order 提交
   - leg mismatch

### 11.4 交付物

- 新 query payload
- 新 UI 卡片/表格
- 新 operator 审计事件

### 11.5 测试

- query service 单测
- dashboard/operator UI 最窄集成测试

### 11.6 阶段红线

以下任一存在，不得进入 Phase 7：

- UI 仍只展示净仓
- operator 无法看见 `posMode` 与双腿状态
- 审计里没有 leg 级事件

## 12. Phase 7：策略 overlay

### 12.1 目标

在前 1-6 阶段完成后，最后再把策略层升级成真正能利用 hedge mode 的交易策略。

### 12.2 范围

- `aats/services/decision_engine/target_position.py`
- `aats/services/decision_engine/orchestrator.py`
- `aats/services/strategy_engines/*`
- 相关 profile/config schema

### 12.3 工作项

分三步做，不要一次到位：

1. `protective`
   - 主腿继续由 directional 主信号决定
   - 对冲腿只在保护性条件下开启
2. `opportunistic`
   - 允许更主动的短线 hedge
3. `independent`
   - 长短两腿可独立决策

### 12.4 交付物

- hedge overlay 配置
- 新决策输出结构
- 不同 overlay 模式的 runtime 行为

### 12.5 测试

- overlay 逻辑单测
- 回放测试
- 最窄运行链集成测试

### 12.6 阶段红线

以下任一存在，不得把 hedge mode 宣称为“可用策略模式”：

- 只有底层双腿，没有策略出口
- 保护性 hedge 没有最小 hold / rebalance cooldown
- independent 模式没有完整风控与恢复覆盖

## 13. 关键依赖关系

### 13.1 强依赖

- Phase 2 依赖 Phase 1
- Phase 3 依赖 Phase 2
- Phase 4 依赖 Phase 3
- Phase 5 依赖 Phase 2、3、4
- Phase 6 依赖 Phase 5
- Phase 7 依赖 Phase 1-6

### 13.2 可并行子项

可以并行的内容：

- Phase 2 的 snapshot schema 与 query payload 适配
- Phase 6 的 UI 原型与 query schema 设计
- Phase 7 的 overlay 参数设计与回放样本整理

不能并行乱做的内容：

- 在 Phase 3 之前改 planner 策略逻辑
- 在 Phase 4 之前启用真实 hedge 下单
- 在 Phase 5 之前把 hedge mode 交给 operator 恢复使用

## 14. 阶段性交付格式

每个阶段交付必须包含：

1. 变更范围
2. 影响模块
3. 数据模型变化
4. 配置变化
5. 测试清单
6. 回滚方式
7. 尚未覆盖的风险

## 15. 阶段验收门

### Gate A

通过条件：

- 账户模式可见
- 启动 fail-fast 可用

### Gate B

通过条件：

- 双腿仓位与快照已成为本地真相

### Gate C

通过条件：

- 腿级订单语义成为唯一合约 hedge mode 下单路径

### Gate D

通过条件：

- 风控四口径齐全，毛敞口不再漏检

### Gate E

通过条件：

- 对账与恢复不会把合法双腿误判为异常

### Gate F

通过条件：

- operator / UI / audit 完整理解双腿

### Gate G

通过条件：

- protective hedge 可实跑
- opportunistic / independent 具备完整测试与回放样本

## 16. 建议的开发顺序与人员切分

### 16.1 建议顺序

推荐按以下里程碑提交：

1. PR-1：Phase 0 + Phase 1
2. PR-2：Phase 2
3. PR-3：Phase 3 + Phase 4
4. PR-4：Phase 5
5. PR-5：Phase 6
6. PR-6：Phase 7 protective
7. PR-7：Phase 7 opportunistic / independent

### 16.2 切分原则

- 账户接入与执行语义一组
- 仓位/快照/对账/恢复一组
- 风控一组
- operator/query/UI 一组
- 策略 overlay 一组

## 17. 里程碑完成定义

### Milestone 1

系统能准确识别：

- 我配置的是 net 还是 hedge
- 交易所实际是 net 还是 hedge
- 不匹配时阻止启动

### Milestone 2

系统本地状态里已经有真正的：

- long leg
- short leg

而不是净仓位伪装成双腿。

### Milestone 3

系统已经可以合法提交：

- `buy + long`
- `sell + long`
- `sell + short`
- `buy + short`

并且内部不会再把它们隐式净额化。

### Milestone 4

系统在：

- 风控
- 对账
- 恢复
- UI

四个层面都已经把双腿当成主语义。

### Milestone 5

策略 finally 能直接做：

- 有空腿时再开多腿
- 有多腿时再开空腿

而不会被解释成“减仓/反手”。

## 18. 不可妥协的红线

以下任何一条如果还存在，项目不能宣称 hedge mode 可用：

- 仍以 `current_position_qty` 作为合约主仓位真相
- 仍以 `target_position_qty` 作为 hedge mode 主目标结构
- planner 仍然默认 signed flip
- 风控仍然只看净敞口
- reconciliation 仍然按 symbol 聚合后比较
- recovery 仍然会把合法另一腿误判成未知仓位
- UI/operator 看不到双腿而只看到净仓

## 19. 下一步建议

实施时建议先从 `Phase 0 + Phase 1` 开始，不要直接跳到策略层。

真正的先手工作应该是：

1. 把 `derivatives_position_mode` 和 `exchange posMode` 做成强约束
2. 把 `DecisionContext` / `PositionTarget` 的双腿替代方案先画出来
3. 明确 `LegOrderIntent` schema
4. 给 reconciliation / recovery 先设计腿级 finding 模型

只有这四步完成，后面的代码改造才不会走偏。

## 20. 阶段进入条件 / 退出条件矩阵

| 阶段 | 进入条件 | 退出条件 | 未满足时的处理 |
| --- | --- | --- | --- |
| Phase 0 | 已确认当前运行域仅讨论 `derivatives`，且已有 `Task 90` 作为目标架构基线 | 新配置项存在，operator 可见 `configured posMode` 与 `exchange posMode`，迁移前检查可运行 | 停在预检阶段，不进入代码主改造 |
| Phase 1 | Phase 0 完成，能稳定读取交易所账户配置 | 启动时已能对 `net / hedge` 做 fail-fast，且不匹配时不会继续跑订单链路 | 不得进入双腿模型改造 |
| Phase 2 | Phase 1 完成，账户模式已经可信 | `PortfolioState`、snapshot、query 基础对象都能保留 long/short 两条腿，不再先净额化 | 不得进入腿级订单语义改造 |
| Phase 3 | Phase 2 完成，仓位主模型已经是双腿 | hedge mode 下单入口只接受显式腿意图，不再允许 signed flip 作为主路径 | 不得进入风控与真实 hedge 下单联调 |
| Phase 4 | Phase 3 完成，执行语义已明确 | 风控已同时看 long / short / gross / net，并能按腿给出 `only_reduce` 约束 | 不得进入 recovery / reconciliation 改造联调 |
| Phase 5 | Phase 2-4 完成，双腿状态、下单、风控都已稳定 | 对账与恢复不会把合法双腿持仓误判为 drift / unknown / external | 不得开放 operator 恢复操作 |
| Phase 6 | Phase 5 完成，主运行链已经 hedge-aware | UI / query / audit / blocker 控制都能完整表达双腿系统 | 不得对外宣称“hedge mode 可运维” |
| Phase 7 | Phase 1-6 全部完成 | `protective` 可实跑，`opportunistic / independent` 具备完整回放、风控和恢复覆盖 | 未完成时只能保留底层能力，不开放策略能力 |

## 21. 必须禁止的半成品状态

### 21.1 账户与执行层

以下状态禁止进入联调或上线环境：

- 已把 `derivatives_position_mode` 设成 `hedge`，但启动时仍不会校验交易所 `posMode`
- `okx_adapter` 已支持 `posSide`，但 planner 仍在用 signed quantity 猜开平语义
- hedge mode 下仍允许提交缺少 `pos_side` 的合约订单

### 21.2 仓位与策略层

以下状态禁止继续向上叠策略：

- `DecisionContext` 仍只有 `current_position_qty` 一个主仓位字段
- `PositionTarget` 仍以 `target_position_qty` 作为 hedge mode 的唯一目标表达
- `target_position.py` 仍把“已有空腿时开多腿”解释成 reversal，而不是新腿动作

### 21.3 风控与恢复层

以下状态禁止开放真实交易：

- 风控只看净敞口，不看 gross notional
- reconciliation 仍按 symbol 聚合后比较，而不是按 `symbol + pos_side`
- recovery 仍会把合法另一腿认成 `without_local_execution_chain`

### 21.4 控制面与运维层

以下状态禁止交付 operator 使用：

- UI 只展示净仓，不展示 long leg / short leg
- operator recovery 无法区分哪一条腿异常
- 审计日志里没有 leg order / leg mismatch / mode mismatch 事件

## 22. 模块级实施清单

### 22.1 Phase 0-1：账户模式与准入护栏

优先改造热点：

- `aats/bootstrap/settings.py`
- `aats/bootstrap/config.py`
- `aats/services/execution_engine/okx_account.py`
- `aats/services/execution_engine/okx_adapter.py`
- `aats/api/routes.py`
- `aats/services/operator/query_service.py`
- `configs/strategy_profiles/derivatives.yaml`
- `configs/strategy_profiles/derivatives_live.yaml`

这些模块的目标是先把“系统要求的模式”和“交易所真实模式”做成强约束，而不是留给后续模块自己猜。

### 22.2 Phase 2：双腿仓位与上下文

确认过的净仓位热点文件：

- `aats/schemas/decision.py`
- `aats/schemas/strategy_runtime.py`
- `aats/services/decision_engine/context_builder.py`
- `aats/services/portfolio_service/positions.py`
- `aats/services/portfolio_service/snapshots.py`
- `aats/services/operator/query_service.py`
- `aats/bootstrap/config.py`

这批文件目前仍把 `current_position_qty` / `target_position_qty` 当作主口径，是双腿改造必须先落刀的地方。

### 22.3 Phase 3：腿级订单语义与执行

执行语义主热点：

- `aats/schemas/execution.py`
- `aats/services/execution_engine/planner.py`
- `aats/services/execution_engine/order_manager.py`
- `aats/services/execution_engine/okx_adapter.py`
- `aats/services/execution_engine/paper_adapter.py`
- `aats/services/execution_control/order_service.py`
- `aats/services/execution_control/shadow.py`

这批模块需要统一切到 `LegOrderIntent -> submit_leg_order()` 的显式链路，禁止再从净仓位推断订单动作。

### 22.4 Phase 4：腿级风控

风控热点：

- `aats/services/governance_engine/risk.py`
- `aats/services/governance_engine/derivatives_live_guard.py`
- `aats/services/governance_engine/recovery_posture.py`
- `aats/schemas/governance.py`

重点不是“再多加几个阈值”，而是让风险决策输出天然能表达 long / short / gross / net 四个维度。

### 22.5 Phase 5：腿级对账与恢复

恢复与对账热点：

- `aats/services/reconciliation_service/comparator.py`
- `aats/services/reconciliation_service/repair.py`
- `aats/services/reconciliation_service/replay.py`
- `aats/services/execution_engine/recovery.py`
- `aats/services/runtime_scope.py`
- `aats/services/operator/query_service.py`

这一阶段的关键是：任何 `exchange long + exchange short` 的合法双边状态，都不能再被求和回净仓后做异常判断。

### 22.6 Phase 6：控制面、查询与审计

控制面热点：

- `aats/services/operator/query_service.py`
- `aats/services/operator/account_queries.py`
- `aats/api/static/modules/views/overview-view.js`
- `aats/api/static/modules/views/risk-view.js`
- `aats/api/static/modules/views/strategy-view.js`
- `aats/services/blocker_control/actions.py`

这里必须保证 UI 和 operator payload 跟底层主模型一致，不允许“底层双腿、页面净仓”的错位状态继续存在。

### 22.7 Phase 7：策略 overlay

策略热点：

- `aats/services/decision_engine/target_position.py`
- `aats/services/decision_engine/orchestrator.py`
- `aats/services/ai_service/prompt_builder.py`
- `aats/services/strategy_engines/coordinator.py`
- `aats/services/strategy_engines/smart_arbitrage/engine.py`

这一阶段才进入“怎么利用 hedge mode 赚钱”的问题；在此之前，所有改造都应优先服务于正确性和可恢复性。

## 23. 推荐 PR 拆分与验收包

### 23.1 PR-1：准入护栏包

包含：

- Phase 0
- Phase 1

验收必须覆盖：

- 配置校验
- 启动 fail-fast
- operator 查询能看到 `configured posMode / exchange posMode / mismatch blockers`

### 23.2 PR-2：双腿状态包

包含：

- Phase 2

验收必须覆盖：

- `PortfolioState` 双腿持仓加载
- snapshot 双腿读写
- query 层不再默认把 long/short 聚合成 signed qty

### 23.3 PR-3：腿级订单与风控包

包含：

- Phase 3
- Phase 4

验收必须覆盖：

- `LegOrderIntent` schema
- planner 到 adapter 的腿级提交流程
- long/short/gross/net 风控门限

### 23.4 PR-4：对账恢复包

包含：

- Phase 5

验收必须覆盖：

- comparator 腿级异常分类
- recovery 腿级恢复归因
- operator recovery 页面腿级展示

### 23.5 PR-5：控制面包

包含：

- Phase 6

验收必须覆盖：

- overview/risk/strategy 页面双腿展示
- blocker / audit / operator action 完整性

### 23.6 PR-6：策略包

包含：

- Phase 7 protective
- Phase 7 opportunistic
- Phase 7 independent

建议拆成至少两次上线：

- 第一次只开放 `protective`
- 第二次再评估是否开放 `opportunistic / independent`

## 24. 完成定义

只有同时满足以下条件，才能把“合约 hedge mode 可用”写进项目能力说明：

- 账户模式不匹配会 fail-fast
- 本地仓位真相是双腿，不是净仓位包装
- 所有合约 hedge mode 订单都带显式 `pos_side`
- 风控、对账、恢复、UI 全部按腿工作
- operator 能看清 long leg / short leg / gross / net / mismatch
- 至少 `protective` overlay 具备回放样本、最窄集成测试和真实运行手册
