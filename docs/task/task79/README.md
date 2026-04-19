# Task79 智能套利全链路修复任务书

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 1. 任务定位

`Task79` 用于收口当前 `smart_arbitrage` 全链路里的剩余缺陷、语义错位和可观测性缺口。

本任务不是继续“补几个 if/else”，而是围绕以下目标做正式修复：

- 保证智能套利候选、sleeve intent、allocator、execution legs、recovery、UI 解释是同一套语义
- 保证配置项真正影响执行行为，而不是停留在展示层
- 保证多 pair、负基差、恢复、回放、operator 页面在边界场景下不自相矛盾
- 明确哪些能力已经正式支持，哪些仍然只允许 `advisory_only`

当前系统已经具备的基础链路：

- `strategy_coordinator -> allocator -> position_target -> execution bundle -> recovery / replay`
- `smart_arbitrage` 包式结构
- `pair_registry / cost_model / leg_planner / state_machine / capability resolver`
- `operator runtime` 和前端策略页展示

但在这条链路上，仍然有几类高风险问题没有完全收口。

## 2. 当前确认的问题

### 2.1 已确认缺陷

1. `execution_modes` 配置解析是 fail-open，不是 fail-closed  
   如果用户把 `execution_modes` 写错，pair 会回退成三种模式都允许，而不是阻断该 pair。

2. 多 pair 聚合候选仍然把不同标的数量直接求和  
   在 `max_concurrent_pairs > 1` 场景下，聚合候选会形成“多标的数量总和 + 单一推荐标的”的混合语义。

3. 多 pair 的 `target_notional` 口径会失真  
   `coordinator` 当前会把聚合候选的 `target_position_qty` 按单一参考价格估算 notional，这对不同 hedge symbol 的组合不成立。

4. blocked 正基差的 headline 仍会误导  
   某些路径下已经是 `blocked`，但 `headline` 仍显示 “Positive basis pair is ready.”。

5. 重复显式 pair 目前是静默去重  
   这降低了重复执行风险，但没有把配置冲突显式暴露给 operator。

### 2.2 高置信风险

1. 已持有 pair 后再修改 `execution_modes`，当前行为没有被正式定义  
   系统可能继续允许 recovery / unwind，也可能让 operator 误以为配置会立即接管旧仓位。

2. 多 pair 聚合候选的 candidate-level 字段可能被上游当成“真实单标的目标”  
   尤其是 `recommended_symbol / target_position_qty / target_notional / delta_position_qty`。

3. 配置、运行时、前端之间虽然基本收口，但仍缺“配置错误显式告警”  
   例如非法模式、重复 pair、共享 scope pair、闲置高级参数等。

## 3. 任务目标

本任务完成后，智能套利应满足以下目标：

### 3.1 行为目标

- pair 配置必须是显式、可校验、可解释的
- 非法配置必须 fail-closed，而不是默认放开
- 新开套利、持有套利、恢复套利、退出套利使用统一状态语义
- 多 pair 要么正式支持并保证语义正确，要么在代码上明确限制

### 3.2 一致性目标

- candidate / sleeve intent / allocation / execution legs / UI 展示使用一致的 pair 语义
- 不允许出现“候选说能做、腿说不能做、UI 说没问题”的分裂状态
- blocked / advisory / opening / active / recovery / unwinding 的 headline、reason code、route action 必须一致

### 3.3 运维目标

- operator 能看出哪条 pair 被选中、为什么被选中、为什么没被选中
- operator 能区分“配置错了”“能力没开”“已有脏仓位”“恢复优先中”“只是基差没到”
- 页面上不再出现误导 headline、原始内部 code、或意义不明的聚合数值

## 4. 非目标

本任务默认不包含以下内容，除非后续子任务显式纳入：

- 多交易所套利
- 跨 runtime 的外部现货数据库状态源
- 引入新的套利家族
- 重写 allocator 全框架
- 重写 execution / replay / ledger 主链

## 5. 全链路范围

本任务覆盖以下模块：

- `aats/services/strategy_engines/smart_arbitrage/*`
- `aats/services/strategy_engines/coordinator.py`
- `aats/services/strategy_engines/allocator.py`
- `aats/bootstrap/settings.py`
- `aats/services/operator/query_service.py`
- `aats/api/static/modules/views/strategy-view.js`
- `aats/api/static/modules/terms.js`
- `configs/strategy_profiles/*.yaml`
- 相关 unit / integration tests

## 6. 关键不变量

修复过程中必须持续验证以下不变量：

1. 配置约束不变量  
   pair 不允许的 execution mode 不能在新开仓路径被实际生成。

2. 状态一致性不变量  
   同一条 pair 在 candidate、sleeve intent、allocation、UI 中的状态含义必须一致。

3. 数量语义不变量  
   单标的字段不能承载多标的聚合语义。

4. 预算和 notional 不变量  
   `target_notional` 必须对应真实可解释的目标腿，不允许用混合标的数量乘单一价格。

5. 恢复优先级不变量  
   已持有 pair 的 recovery / unwind 不能因为配置展示层调整而被意外阻断。

6. UI 解释不变量  
   `headline / reason_codes / blocking_reasons / legs` 不能互相打架。

## 7. 重点排查点

### 7.1 Pair Registry 与配置解析

重点排查：

- `pair_definitions` 是否存在重复 scope
- `pair_id` 冲突时如何处理
- `execution_modes` 是否严格校验
- 默认 derived pair 是否只在必要时注入

最容易出问题的点：

- 非法模式被静默修正成默认全开
- 重复 pair 被静默吞掉但 operator 无感知
- 显式 pair 和 derived pair 的优先级不清晰

### 7.2 Opportunity / Candidate 生成

重点排查：

- `execution_modes` 是否只限制 opening，还是也影响 recovery / unwind
- blocked 候选的 headline 是否正确
- `reason_codes` 是否能覆盖所有阻断路径

最容易出问题的点：

- state 已 blocked，但 headline 仍显示 ready
- `reason_codes` 正确，UI 却读了 headline
- 配置切换后旧仓位语义不清

### 7.3 多 Pair 聚合

重点排查：

- candidate-level 聚合字段是否仍假设“单一标的”
- `recommended_symbol / target_position_qty / delta_position_qty / target_notional` 的聚合是否合理
- overlap-safe 逻辑是否只作用于 opening，active/recovery pair 是否仍可能冲突

最容易出问题的点：

- 多标的 quantity 被直接相加
- 用单一参考价估算多标的 notional
- allocator 或 UI 把聚合候选当成单标的 candidate 继续消费

### 7.4 Coordinator / Allocator / Sleeve Intent

重点排查：

- `smart_arbitrage` 聚合候选进入 sleeve intent 后的字段语义
- `target_notional` 是否仍从 candidate-level 单值推导
- `strategy_sleeve_id` 与 `symbol_scope` 是否能唯一表达多 pair

最容易出问题的点：

- 聚合 candidate 污染 budget 分配
- `intent.symbol` 只保留顶部 hedge symbol
- allocation summary 与实际 legs 不一致

### 7.5 UI / Operator Runtime

重点排查：

- 主候选表、recent sleeve intents、运行摘要、配置卡是否都用同一解释口径
- reason code 是否全部有本地化映射
- 高级参数是否展示“生效”而不是“仅配置值”

最容易出问题的点：

- 主表正确，recent intent 仍显示英文 headline
- 配置卡说“模式未开放”，但后端仍能开仓
- 前端把聚合 candidate 当单标的显示

## 8. 分阶段任务拆解

## 8.1 Task79-A0 基线锁定

目标：

- 在正式修复前锁定当前行为和已确认缺陷

工作项：

- 记录当前已确认缺陷清单
- 为最小复现场景补回归测试草案
- 明确哪些行为是“现状兼容”，哪些是“必须改变”

验收：

- 有一份可执行的缺陷清单
- 新增测试先红后绿

## 8.2 Task79-A1 修复 `execution_modes` fail-open

目标：

- 非法或未知 `execution_modes` 配置必须 fail-closed

工作项：

- 修改 `pair_registry` 解析逻辑
- 明确区分：
  - 未配置 `execution_modes`：使用默认允许集
  - 已配置但为空或全非法：标记 pair 无可执行模式并附 blocking reason
- 把非法配置显式暴露给 operator/runtime

重点排查：

- 是否影响旧配置兼容
- 是否会把已有 live profile 全部误判为非法

验收：

- 非法 mode 不再回退成三种全开
- operator 能看到“pair 配置非法”

## 8.3 Task79-A2 收口重复 pair 和冲突配置

目标：

- 重复 scope pair 不再静默吞掉

工作项：

- 对 `(spot_symbol, hedge_symbol)` 重复做显式告警
- 对 `pair_id` 冲突做显式告警
- 决定优先级：
  - 显式 pair 优先于 derived pair
  - 首条显式 pair 生效还是整组阻断

重点排查：

- 配置冲突是否会污染 UI pair 数
- 是否会影响已有 runtime 对显式 pair 的读取顺序

验收：

- 冲突配置在 operator/runtime/UI 上可见
- 不再只有静默去重

## 8.4 Task79-A3 定义 `execution_modes` 对 active/recovery/unwind 的语义

目标：

- 明确配置变更后，已持有 pair 的处理策略

工作项：

- 定义并实现以下规则：
  - `opening` 受 `execution_modes` 约束
  - `recovery / unwind` 是否允许继续接管已有 pair
  - 若不允许，系统应该如何告警
- 把该语义写入 reason code 和 operator 文案

重点排查：

- 不能因为配置改窄，导致旧 pair 无法退出
- 不能让 operator 误以为“配置一改，旧仓位自动失效”

验收：

- active pair 在配置变更后的行为有明确且可测试的定义

## 8.5 Task79-A4 修复多 pair 聚合语义

目标：

- 多 pair 候选不再伪装成单标的 candidate

工作项：

- 重新设计聚合候选字段
- 明确哪些字段在 multi-pair 下必须为空、改名、或改为聚合结构
- 禁止 `recommended_symbol + summed target_position_qty` 这种混合语义

推荐方向：

- 单 pair：保留现有 candidate-level 数值
- 多 pair：用 `selected_pairs[]` + `aggregate_metrics` + `legs[]`
- 对 `target_position_qty / delta_position_qty / target_notional` 采用保守策略：
  - 要么不填
  - 要么只表达“主 pair”
  - 要么新增专门的 aggregate 字段

重点排查：

- allocator 是否依赖这些字段
- UI 是否默认把它们当单标的字段

验收：

- 不再出现“多标的数量总和 × 单一价格”的 notional

## 8.6 Task79-A5 修复 coordinator / sleeve intent / allocation 的多 pair 口径

目标：

- `smart_arbitrage` 进入 coordinator 后仍保持正确语义

工作项：

- 修复 `_build_sleeve_intents()` 对 multi-pair 的处理
- 修复 `target_notional` 的生成逻辑
- 校验 `symbol_scope`、`strategy_sleeve_id`、`legs` 是否仍然自洽

重点排查：

- multi-pair 是否应该继续共享一个 sleeve
- budget profile 是否应基于腿级 notional，而不是 candidate-level 单值

验收：

- multi-pair 不再污染 `intent.symbol / intent.target_notional`

## 8.7 Task79-A6 修复 blocked / advisory headline 与 reason 展示

目标：

- headline、reason、state 在所有展示路径一致

工作项：

- 修正 `_headline_for()` 的 blocked 分支
- 收口 `recent sleeve intents`、配置页、策略页、operator summary 的展示逻辑
- 明确“headline 是概括，reason 是解释”的使用边界

重点排查：

- 主候选表已经正确，但其它表仍读旧 headline
- 英文 headline 从其它路径泄漏出来

验收：

- blocked 正基差不再出现 “pair is ready”
- reason code 漏映射全部补齐

## 8.8 Task79-A7 收口配置面与 profile

目标：

- YAML、settings、operator runtime、UI 使用同一套 smart arbitrage 配置面

工作项：

- 补齐 profile 缺失字段或删除闲置字段
- 把“已配置但当前不生效”的高级参数明确标注
- 明确推荐值和生产安全默认值

重点排查：

- `max_concurrent_pairs`
- `pair_priority_mode`
- `cost_model_enabled`
- `funding_cost_enabled`
- `borrow_cost_enabled`
- `margin_short_auto_repay_enabled`

验收：

- profile 文件与 runtime 暴露字段一致
- UI 不再只展示部分高级配置

## 8.9 Task79-A8 补齐测试矩阵

目标：

- 用测试把这次修复正式钉住

必须新增的测试：

- 非法 `execution_modes` fail-closed
- 重复显式 pair 冲突告警
- 配置变更后 active pair 的 recovery / unwind 行为
- multi-pair candidate 不再生成失真 `target_notional`
- blocked 候选在 recent sleeve intents 里的 headline
- UI 对新增 reason code 的完整映射

验收：

- unit / integration 至少覆盖上述场景

## 8.10 Task79-A9 回放、恢复与 operator 验证

目标：

- 确认智能套利修复没有破坏 replay / recovery / operator 页面

工作项：

- 跑 `persistence_and_replay`
- 跑 `recovery`
- 跑 `strategy_runtime_integration`
- 跑 `dashboard_ui / operator_api`

重点排查：

- multi-pair 或 blocked pair 是否污染 runtime payload
- recovery 后 UI 是否仍能解释当前状态

验收：

- 智能套利相关集成回归通过

## 9. 实施优先级

按生产风险排序：

1. `Task79-A1` 修复 `execution_modes` fail-open
2. `Task79-A4` 修复多 pair 聚合语义
3. `Task79-A5` 修复 coordinator / intent / notional 口径
4. `Task79-A6` 修复 headline / reason 展示错位
5. `Task79-A3` 明确 active pair 配置变更语义
6. `Task79-A2` 收口重复 pair 冲突告警
7. `Task79-A7` 收口配置面
8. `Task79-A8` 补测试
9. `Task79-A9` 跑恢复/回放/前端回归

## 10. 验收标准

满足以下条件才算完成：

1. 非法 `execution_modes` 不再默认放开
2. multi-pair 不再产出失真 candidate-level notional
3. blocked / advisory / active / recovery / unwind 的 headline 与 reason 一致
4. 配置文件、runtime payload、UI 配置卡字段一致
5. 智能套利相关 unit / integration 测试通过
6. replay / recovery / operator / dashboard 相关回归通过

## 11. 上线前检查表

- `smart_arbitrage_max_concurrent_pairs` 在正式修复前是否仍保持 `1`
- live profile 是否存在重复 pair
- live profile 是否存在非法 `execution_modes`
- operator 页面是否还能看到英文 headline 或原始 reason code
- multi-pair candidate 是否仍在 UI 中显示单一 `recommended_symbol + summed qty`
- recovery / replay 回归是否已重新跑绿

## 12. 建议的交付顺序

建议分 3 个批次交付：

### 批次 1：保命修复

- A1
- A4
- A5

### 批次 2：解释与配置收口

- A6
- A2
- A7

### 批次 3：验证与上线

- A3
- A8
- A9

---

这份任务书的核心原则是：

- 先修“会让系统做错事”的链路
- 再修“会让 operator 看错”的链路
- 最后再收口配置和文档

如果后续决定继续把智能套利扩到真正的多 pair 生产模式，这份任务书里的 A4/A5/A9 应视为上线前硬门槛，而不是可选优化。
