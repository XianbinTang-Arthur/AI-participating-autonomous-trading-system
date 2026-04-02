# Task81 系统多币种改造任务书

## 1. 任务定位

`Task81` 用于把当前以“单主标的 runtime”为中心的交易系统，升级成支持多币种运行的架构。

本任务分两个阶段推进：

1. 最小可落地版
   - 支持多个 symbol 同时订阅、决策、执行、恢复和展示
   - 每个 symbol 仍按“独立决策链”工作
   - 风险控制和预算仍以单 symbol 或单 sleeve 为主

2. 组合化完整版
   - 支持跨 symbol 的统一预算、统一风控、统一试盘审查、统一恢复和统一归因
   - 从“多币并行”升级成“多币组合交易系统”

本任务不要求第一阶段就实现组合级最优分配。
第一阶段的目标是“稳定跑多个币”，不是“做投资组合优化器”。

## 2. 当前系统现状

### 2.1 已有多币基础能力

当前系统并不是纯单币死结构，已经有一些可复用基础：

1. 配置层已经支持 `allowed_symbols`
   - 见 [settings.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/settings.py)

2. 决策触发按 `symbol + timeframe` 独立运行
   - 见 [trigger.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/trigger.py)

3. `DecisionContext` 本身是单 `symbol`
   - 见 [decision.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/schemas/decision.py)

4. 市场网关、事件回放、runtime scope、对账与恢复很多地方已支持 `allowed_symbols`
   - 见 [config.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/config.py)
   - 见 [runtime_scope.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/runtime_scope.py)
   - 见 [event_store_postgres.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/storage/event_store_postgres.py)

5. 智能套利已经有多 pair / `symbol_scope` 的基础
   - 见 [engine.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/smart_arbitrage/engine.py)

### 2.2 当前仍然偏单币的关键位置

1. `default_symbol` 仍然是很多查询、展示和默认行为的中心
   - 见 [settings.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/settings.py)

2. `StrategyCoordinatorService.evaluate()` 当前仍以一个 `context.symbol` 为主入口
   - 见 [coordinator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/coordinator.py)

3. `allocator` 主结果仍围绕 `base_target.symbol` 汇总
   - 见 [allocator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/allocator.py)

4. operator / UI 仍存在“当前主标的”视角
   - 见 [query_service.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py)
   - 见 [strategy-view.js](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/api/static/modules/views/strategy-view.js)

5. 一些账户/费用/优化查询仍默认读取 `default_symbol`
   - 见 [okx_account.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_account.py)
   - 见 [strategy_profile_optimization.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/strategy_profile_optimization.py)

## 3. 核心设计原则

1. 第一阶段坚持“每个 symbol 独立决策链”
   - 不把多币改造成单次决策输出多个 symbol 的超级上下文

2. 第二阶段才引入“组合级决策与治理”
   - 跨 symbol 预算
   - 组合风险上限
   - 组合级试盘审查

3. symbol scope 必须成为一等公民
   - 配置、事件、回放、执行、恢复、UI 都必须显式带 `symbol_scope`

4. 多币运行不能破坏现有单币 invariants
   - 订单生命周期
   - 余额完整性
   - 幂等 / replay
   - 恢复安全

5. 先做“能稳定跑”，再做“组合最优”

## 4. 分阶段目标

## 4.1 阶段一：最小可落地版

### 目标

- 一个 runtime 支持多个 `allowed_symbols`
- 每个 symbol 独立触发决策
- 每个 symbol 独立生成候选、sleeve、allocation、执行 bundle
- UI 和 operator 可按 symbol 查看状态
- recovery / replay / 对账按多 symbol scope 正常工作

### 非目标

- 不做跨 symbol 的统一预算优化
- 不做组合 Sharpe / 相关性 / 风险平价
- 不做跨 symbol 的试盘守护评分融合

## 4.2 阶段二：组合化完整版

### 目标

- 多 symbol 共享资金池
- 跨 symbol 统一 allocator
- 组合级风控、审查、恢复、归因、预算
- UI 支持组合视角和单币视角双展示

### 非目标

- 不要求第一版就实现高级投资组合优化算法
- 不要求一开始就做跨交易所组合统一治理

## 5. 关键不变量

1. `state consistency`
   - 同一 symbol 的 `DecisionContext -> candidate -> sleeve intent -> allocation -> bundle -> UI` 必须一致
   - 多 symbol 并行时不能互相串状态

2. `balance/accounting integrity`
   - 每个 symbol 的委托、成交、持仓、费用、归因必须仍能按 scope 追溯
   - 多币不能让余额被重复占用或遗漏释放

3. `idempotency/retry safety`
   - 同一 symbol 的命令、订单、fill、恢复流程仍需幂等
   - 不能因为多币并发导致同一命令被重复执行

4. `correct order lifecycle behavior`
   - 每个 symbol 的开仓、撤单、fill、recovery 语义不变
   - 多币只是并行增加，不改变单币生命周期语义

5. `scope integrity`
   - event store / repo / recovery / UI 必须清楚区分：
     - 单 symbol
     - symbol_scope
     - runtime_scope

## 6. 重点排查与易错点

### 6.1 配置层

重点排查：
- `default_symbol` 是否仍被隐式当成“唯一交易标的”
- `allowed_symbols` 是否只在订阅层生效，没真正进入决策链
- `smart_arbitrage_pair_definitions` 是否能跨多个主标的一起工作

易错点：
- 配置里写了多个 symbol，但 UI / runtime 仍只展示一个
- profile 模板支持多币，settings 校验却仍默认单币

### 6.2 决策触发层

重点排查：
- feature snapshot 到 decision cycle 是否按每个 symbol 独立触发
- `DecisionTriggerPolicy` 的节流是否按 `symbol + timeframe`

易错点：
- 多 symbol 共用一把锁
- 不同 symbol 的触发计数互相污染

### 6.3 上下文与策略层

重点排查：
- `DecisionContextBuilder` 是否始终用正确 symbol 构建上下文
- 各策略是否偷偷读取 `default_symbol`

易错点：
- DCA / 网格 / 方向策略仍按 `default_symbol` 查仓位
- 智能套利 pair registry 在多币配置下推导错腿

### 6.4 协调与 allocator

重点排查：
- 当前 allocator 是“单 symbol 主控”还是“多 symbol 并行”
- `StrategyCoordinatorSnapshot` 是否能同时承载多个 symbol 的 sleeve 记录

易错点：
- 多 symbol 候选混成一条 allocation
- `target_notional` / `approved_notional` 把不同 symbol 错汇总

### 6.5 执行与恢复

重点排查：
- order manager / execution repo / recovery service 是否按 scope 正确过滤
- baseline import / replay / reconciliation 是否能处理多 symbol 并存

易错点：
- 恢复时只看 `default_symbol`
- 多币 runtime 下 orphan orders / obligations 被错误归属

### 6.6 账户与市场数据

重点排查：
- OKX account refresh 是否覆盖全部 tracked symbols
- market gateway 是否对 `allowed_symbols` 全量订阅并正确缓存

易错点：
- 只订阅了主 symbol，其他 symbol 没快照
- 费用、持仓、funding 摘要只按主 symbol 刷新

### 6.7 UI 与 operator

重点排查：
- 当前页面是不是默认只展示 latest selected family / latest selected symbol
- 是否存在多币下“主卡片覆盖其他币”的问题

易错点：
- 只看到一个 symbol，以为系统没跑其他币
- recovery / trial guard / attribution 只显示主标的结果

## 7. 阶段一任务拆分：最小可落地版

### Task81-A0 基线锁定

目标：
- 盘点当前哪些模块是“天然多币”
- 盘点哪些模块仍默认单币

产出：
- 多币兼容矩阵
- 单币隐式依赖清单

### Task81-A1 配置模型升级

目标：
- 明确 `default_symbol` 和 `allowed_symbols` 的职责边界

要求：
- `default_symbol` 仅作为默认视图 / 主参考 symbol
- `allowed_symbols` 作为真正运行标的列表
- `expanded_allowed_symbols()` / `decision_cycle_symbols()` 语义明确

验收：
- 多币 profile 可被 settings 正确加载
- 不写 `allowed_symbols` 时仍兼容单币

### Task81-A2 决策触发多币化

目标：
- 每个 symbol 独立触发 decision cycle

要求：
- 节流、锁、时间框架都按 `symbol + timeframe`
- 不能存在多 symbol 互相阻塞

验收：
- 两个 symbol 的 feature snapshot 可各自触发决策
- 不会串 decision_id / snapshot ref

### Task81-A3 决策上下文收口

目标：
- 清理所有策略和上下文中对 `default_symbol` 的隐式读取

要求：
- `DecisionContextBuilder`、各 strategy engine、target engine 统一以 `context.symbol` 为主
- 智能套利只在明确 pair scope 下扩 symbol，不影响其他 family

验收：
- 多币下方向、定投、网格、智能套利均可独立运行

### Task81-A4 协调与 sleeve 多币化

目标：
- `StrategyCoordinatorService` 可在同一 runtime 下记录多个 symbol 的 sleeve

要求：
- `strategy_sleeve_id` 和 `symbol_scope` 在多币下稳定唯一
- recent intents / runtime snapshot 能区分 symbol

验收：
- operator 可看到多 symbol sleeve 记录

### Task81-A5 allocator 安全边界

目标：
- 第一阶段不做组合优化，但必须明确 allocator 边界

要求：
- 仍允许按单 symbol allocation
- 不允许错误把不同 symbol 聚成一条单 symbol allocation
- 明确声明第一阶段是“多币并行、非组合化”

验收：
- 多 symbol 下 allocation 结果不串账

### Task81-A6 执行与恢复 scope 加固

目标：
- order / fill / bundle / obligation / recovery 全部支持多 symbol 并存

要求：
- repo 查询必须使用 `allowed_symbols` / `symbol_scope`
- recovery 不再默认只看 `default_symbol`

验收：
- 多币下重启后恢复不串 symbol
- replay / reconciliation scope 正确

### Task81-A7 Operator / UI 多币视图

目标：
- operator 页面和核心面板支持按 symbol 查看

要求：
- 提供：
  - runtime 总览
  - symbol 维度卡片
  - strategy sleeve 维度卡片
- 不再只围绕单一主 symbol 展示

验收：
- 页面能看到多个 symbol 的候选、执行、恢复、归因摘要

### Task81-A8 多币测试矩阵

目标：
- 为阶段一建立最小可信验证

必须覆盖：
- 双 symbol feature snapshot 触发
- 双 symbol 决策结果独立
- 双 symbol 执行与恢复独立
- UI / operator 能同时显示多个 symbol
- replay / reconciliation 支持多 symbol scope

## 8. 阶段二任务拆分：组合化完整版

### Task81-B0 组合模型基线

目标：
- 定义“组合级状态”而不是简单多个 symbol 并排

需要明确：
- 总资金池
- 每 symbol 风险暴露
- 组合净暴露
- 组合 drawdown
- 组合试盘审查口径

### Task81-B1 组合级 allocator

目标：
- 把 allocator 从“单 symbol 主控”升级成组合分配器

要求：
- 支持多个 symbol 竞争同一预算
- 支持 family 间、symbol 间统一优先级
- 支持组合总名义金额 / 保证金 / 风险上限

### Task81-B2 组合级风险引擎

目标：
- 风险不再只看单 symbol

要求：
- 支持：
  - 总方向暴露
  - 同向集中度
  - 相关性集中过高
  - 总杠杆 / 总保证金占用

### Task81-B3 组合级试盘审查与 trial guard

目标：
- trial guard 从单 symbol 扩展到组合级

要求：
- 区分：
  - 单币恶化
  - 组合恶化
- 明确 reset / override 的组合口径

### Task81-B4 组合级恢复与对账

目标：
- recovery / reconciliation 支持组合级审查

要求：
- 可分别看：
  - 单 symbol 问题
  - 组合总问题
- resume blocker 支持组合视角

### Task81-B5 组合级归因与 PnL

目标：
- 归因不再只到单 symbol，而是支持组合视角

要求：
- 支持：
  - family 归因
  - symbol 归因
  - 组合总归因
- 支持预算使用率和机会成本分析

### Task81-B6 组合级 UI

目标：
- UI 同时支持：
  - 组合总览
  - symbol drill-down
  - sleeve drill-down

要求：
- 默认看到组合
- 可点进单币细节

## 9. 模块影响范围

第一阶段重点影响：
- [settings.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/settings.py)
- [config.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/config.py)
- [trigger.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/trigger.py)
- [context_builder.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/context_builder.py)
- [coordinator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/coordinator.py)
- [allocator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/allocator.py)
- [runtime_scope.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/runtime_scope.py)
- [query_service.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py)
- [strategy-view.js](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/api/static/modules/views/strategy-view.js)

第二阶段重点影响：
- risk / trial guard / reconciliation / attribution / allocator / operator dashboard 全面升级

## 10. 实施优先级建议

### 第一批必须先做

1. A0 基线锁定
2. A1 配置模型升级
3. A2 决策触发多币化
4. A3 决策上下文收口
5. A6 执行与恢复 scope 加固

### 第二批再做

6. A4 协调与 sleeve 多币化
7. A5 allocator 安全边界
8. A7 Operator / UI 多币视图
9. A8 多币测试矩阵

### 第三批再进入组合化

10. B0 组合模型基线
11. B1 组合级 allocator
12. B2 组合级风险引擎
13. B3 组合级试盘审查
14. B4 组合级恢复与对账
15. B5 组合级归因
16. B6 组合级 UI

## 11. 上线门槛

### 阶段一上线门槛

必须满足：
- 至少 2 个 symbol 在同一 runtime 内可独立决策
- 执行 / replay / recovery / reconciliation 不串 symbol
- operator 页面可以清楚看到多 symbol 状态
- 全量测试和多币切片回归通过

### 阶段二上线门槛

必须满足：
- allocator 已是组合级
- 风控和 trial guard 已是组合级
- recovery / reconciliation / attribution 已能输出组合总览
- operator 可区分单币异常和组合异常

## 12. 最终建议

结论很明确：

1. 先做阶段一
   - 这是“中等改动，风险可控”
   - 能最快把系统从单主标的推进到多币并行

2. 阶段二单独立项
   - 这是“系统级升级”
   - 不应该和阶段一混在一次改造里完成

推荐执行顺序：
- 先把系统改成“多币独立运行”
- 再评估是否值得继续投入到“组合化完整版”
