# Task72-A 合约优先版独立实施清单

## 1. 文档定位

这份清单用于把当前系统升级成“可在强约束下运行合约实盘”的系统。

它是独立实施清单，不要求先完成现货版。  
当前项目如果只启动一条主线，应优先按本清单执行。

## 2. 成功定义

合约优先版完成后，系统至少要达到以下状态：

- 能在单账户、单 runtime、单产品线约束下运行合约 `guarded_live`
- 能正确建模 `tdMode`、`posSide`、杠杆、风险限额、只减仓和清算距离
- 能在重启、乱序、重复回报、REST / WS 抖动下恢复订单与持仓状态
- 能对持仓、保证金、已实现盈亏、未实现盈亏、手续费、资金费做可审计重建
- 能在风险超限、状态不确定、对账失败时自动停机并阻断继续开仓

## 3. 适用边界

### In Scope

- OKX 合约实盘
- cross / isolated 保证金模式
- net / long-short 持仓方向语义
- 合约下单前校验、执行状态机、恢复、对账、自动停机
- 合约保证金、杠杆、仓位、风险限额、资金费、PnL 审计链路
- operator / 控制面 / 报警 / 报表 / UTF-8 中文文案

### Out of Scope

- 多交易所合约统一层
- 组合保证金等更高级账户模式
- 自动出入金、自动资金调拨
- 高频撮合级策略
- 未通过 `guarded_live` 小资金验证前的高自动化放量

## 4. 实施原则

- 所有阶段默认按“失败即阻断”设计，而不是“失败后继续尝试交易”
- 不允许把现货余额语义直接复用为合约保证金语义
- 不允许让交易所成为第一道校验，系统必须先完成本地 pre-trade gate
- 不允许在缺少恢复与对账闭环的情况下打开实盘提交
- 所有新增前端、控制面和报警文案必须是干净的 UTF-8 中文

## 5. 阶段化实施清单

### 阶段 0：共享底座与生产约束

目标：先把当前 runtime 变成可控、可隔离、可审计的真钱运行底座。

主要落点：

- `aats/bootstrap`
- `configs`
- `aats/services/operator`
- `aats/api`
- `aats/storage`
- `tests`

必做清单：

- [ ] 固化 `.env.derivatives`、runtime profile、YAML 配置和代码默认值的优先级
- [ ] 固化单 runtime、单账户、单环境、单产品线的启动约束
- [ ] 为生产密钥注入、轮换、失效和审计建立明确流程
- [ ] 增加启动前自检，未满足账户模式、数据库、时钟、关键配置条件时禁止起盘
- [ ] 为 operator 高风险动作增加鉴权、二次确认和完整审计
- [ ] 明确 `guarded_live`、`autonomous_live`、只读、恢复态四类运行口径
- [ ] 把所有新增前端 / operator 文案纳入 UTF-8 中文词典，不允许后端裸字段直出

完成标志：

- 可以给出一份合约生产部署清单
- 任一实例违反单账户或单产品线约束时无法进入交易态
- 高风险动作都能在审计记录中追溯到人、时间、原因和结果

### 阶段 1：合约产品模型与账户模式建模

目标：把“合约只是另一种 symbol”升级成“合约有独立账户语义和产品规则”的模型。

主要落点：

- `aats/services/market_gateway`
- `aats/schemas`
- `aats/services/execution_engine`
- `aats/services/operator`
- `aats/storage`

必做清单：

- [ ] 拉取并持久化合约产品规则：`tick size`、`lot size`、`contract value`、最小下单量、最大下单量
- [ ] 拉取并持久化账户模式：`tdMode`、`posMode`、杠杆、保证金币种、可用保证金
- [ ] 在订单意图中显式建模 `tdMode`、`posSide`、`reduceOnly`、`closeOnly`
- [ ] 对账户模式和订单意图做一致性校验，不一致时本地直接阻断
- [ ] 明确 net 与 long-short 模式下的仓位聚合方式和对账方式
- [ ] 明确 cross 与 isolated 模式下的风险视图和持仓视图边界

完成标志：

- 任一合约订单在提交前都能回答自己使用的账户模式与产品规则是什么
- operator 页面能看到产品规则快照与账户模式快照

### 阶段 2：合约下单前风控与限仓体系

目标：在订单提交前就拦住高风险行为，而不是依赖交易所 reject。

主要落点：

- `aats/services/execution_engine`
- `aats/services/portfolio_service`
- `aats/services/ledger`
- `aats/services/operator`
- `aats/storage`
- `tests`

必做清单：

- [ ] 计算开仓前名义价值、保证金占用、已挂单风险暴露和剩余可开仓额度
- [ ] 建立按账户、按产品、按方向的最大仓位和最大挂单暴露限制
- [ ] 建立最大杠杆、最大单笔 notional、最大日内亏损、最大连续失败次数限制
- [ ] 建立只减仓模式和禁开仓模式，在高风险状态下自动切换
- [ ] 在资金费窗口、快照陈旧、持仓不确定、对账失败时阻断新开仓
- [ ] 给每个阻断场景定义稳定的 reason code 和 UTF-8 中文说明
- [ ] 增加单元测试和集成测试，覆盖跨模式、跨方向、重复开平仓和风险超限场景

完成标志：

- 任何被拒绝的订单都能回答“哪条风控规则阻断了它”
- 即使交易所允许提交，本地风险 gate 也能先行阻断不安全订单

### 阶段 3：合约执行状态机与恢复硬化

目标：让真实合约订单在提交、确认、部分成交、撤单、超时、重启和重放场景下都能收敛。

主要落点：

- `aats/services/execution_engine`
- `aats/services/recovery_control`
- `aats/services/reconciliation_service`
- `aats/storage`
- `tests`

必做清单：

- [ ] 明确 private WS 与 REST 补拉的状态优先级和冲突处理规则
- [ ] 明确 submit 成功但 ack 丢失、cancel 发出但未确认、order not found 等恢复语义
- [ ] 为 client order id、command id、fill id 建立完整幂等策略
- [ ] 防止重复 fill、重复撤单、乱序回报导致重复结算或错误释放风险
- [ ] 启动恢复时自动识别未知中间态订单，并进入 review 或 halt，而不是继续下单
- [ ] 补齐跨进程重启、事件重放、交易所抖动的集成回归测试

完成标志：

- 任一活跃订单在系统重启后都能恢复到可解释状态
- 不存在重复 fill 导致的重复记账或重复释放保证金

### 阶段 4：合约持仓、保证金、PnL 与资金费账务闭环

目标：让合约实盘下的持仓和资金变化都能进入一条可审计的财务链路。

主要落点：

- `aats/services/ledger`
- `aats/services/portfolio_service`
- `aats/services/reconciliation_service`
- `aats/storage`
- `tests`

必做清单：

- [ ] 建立合约持仓投影，明确数量、均价、方向、保证金模式和杠杆信息
- [ ] 在 `long_short_mode` 下按仓位侧隔离本地持仓与 lot，不允许只按 `symbol` 聚合
- [ ] 区分并持久化已实现盈亏、未实现盈亏、手续费、资金费
- [ ] 建立保证金占用、释放、转移和风险缓冲的账务语义
- [ ] 建立 cross / isolated 两种模式下的财务投影与对账规则
- [ ] 确保订单、成交、持仓、保证金、journal、entry 之间可相互追溯
- [ ] 为资金费记账、结算和报表建立独立字段与回归测试
- [ ] 提供按订单、按持仓、按结算周期追踪 PnL 的视图或接口

完成标志：

- 可以从系统数据重建任一时刻的持仓、保证金和 PnL
- 资金费、手续费、成交盈亏不会落在“只能看交易所页面”的黑盒里

### 阶段 5：合约对账、自动停机与人工恢复

目标：把“系统不确定”明确转成“系统停机并要求人工接管”。

主要落点：

- `aats/services/reconciliation_service`
- `aats/services/recovery_control`
- `aats/services/operator`
- `aats/api`
- `tests`

必做清单：

- [ ] 对账户快照陈旧、持仓不一致、保证金不一致、资金费不一致、fill 不一致做分类
- [ ] 明确 halt、review_required、resume_blocked、only_reduce 四类状态
- [ ] 明确 rebaseline、resume、force resolve 的前置条件、审批要求和审计记录
- [ ] 在清算距离过近、保证金率异常、关键依赖持续失败时自动停机
- [ ] 保证恢复流程先拿到新鲜快照，再切换系统状态
- [ ] 所有异常状态都要在 operator 页面给出 UTF-8 中文解释与建议动作

完成标志：

- 对账失败后系统不会继续开新仓
- operator 可以根据控制面信息完成一次完整的审查、恢复或继续停机决策

### 阶段 6：观测、报警与控制面收口

目标：让合约实盘的风险和运行质量可见、可读、可追踪。

主要落点：

- `aats/services/operator`
- `aats/api`
- `aats/api/static`
- `aats/storage`
- `tests`

必做清单：

- [ ] 增加保证金率、清算距离、持仓暴露、订单失败率、撤单率、恢复次数、对账漂移等指标
- [ ] 建立合约日报、周报和事故复盘报表
- [ ] 建立关键报警：私有 WS 中断、快照超时、风险超限、对账失败、重复回报、停机触发
- [ ] 在控制面中按账户、产品、方向展示当前风险状态和最近异常
- [ ] 建立统一前端词典，所有状态、风险、建议动作统一为干净 UTF-8 中文

完成标志：

- 不需要登录交易所网页，也能从本系统判断当前是否安全运行
- 关键风险信号都具备自动报警和可追溯时间线

### 阶段 7：合约小资金 `guarded_live` 试运行

目标：在严格限制下完成第一轮真钱前向验证。

主要落点：

- `configs`
- `aats/bootstrap`
- `aats/services/operator`
- `aats/services/blocker_control`
- `tests`

必做清单：

- [ ] 限制交易对数量、单笔 notional、最大总暴露和最大持仓天数
- [ ] 初始阶段只允许 `guarded_live`，不允许直接进入更高自动化级别
- [ ] 建立每日开盘前检查、盘中巡检、收盘后复盘流程
- [ ] 建立自动停机、人工复核、继续运行、缩容、回退四类判定规则
- [ ] 连续记录真实成交、滑点、手续费、资金费、恢复事件和报警事件
- [ ] 小资金阶段未通过前，不允许扩大资金量或增加交易对

完成标志：

- 形成一份连续前向验证报告
- 形成一份是否允许放量的书面结论

### 阶段 8：自动调参二期与 operator 解释收口

目标：把自动换档从“只改信号阈值”升级成“能自动收缩风险预算、执行侵略性与交易节奏，并能向 operator 清楚解释为什么切换”的控制层。

主要落点：

- `aats/services/operator`
- `aats/services/governance_engine`
- `aats/services/execution_engine`
- `aats/services/decision_engine`
- `aats/api`
- `aats/api/static`
- `tests`

必做清单：

- [ ] 为 `execution_degraded_safe / high_volatility_defensive / range_defensive` 建立紧急安全换档快速通道
- [ ] 把常规优化换档和紧急安全收缩换档拆成两套门槛
- [ ] 新增风险预算 multiplier，并联动下单量、仓位上限、名义金额上限和合约默认杠杆
- [ ] 新增执行侵略性 multiplier，并联动 passive bias、maker / taker 偏好、切片和 cancel-replace 耐心
- [ ] 把最小持仓、平仓后冷却和低边际冷却纳入策略档位 payload
- [ ] 为 operator 提供当前档位、候选档位、blocked reasons、evidence counters 和 next eligible switch 时间
- [ ] 保证自动调参只会在人工硬上限之内收紧，不会突破人工设定的风险边界

完成标志：

- 系统在运行不稳时能快速切入更保守档位
- 风险预算和执行侵略性会随着运行状态自动收缩
- operator 能一眼看懂当前档位、候选档位和切换阻断原因

## 6. 不允许跳过的硬门槛

以下门槛任何一项未满足，都不允许进入合约真钱提交：

- 无法本地计算并验证杠杆、保证金占用和可开仓额度
- 无法在系统内解释持仓、PnL、资金费和手续费变化
- 无法在重复 fill、重启、WS 中断场景下防止重复记账
- 无法在风险超限或状态不确定时自动停机
- operator 页面无法用清晰 UTF-8 中文表达当前风险和建议动作

## 7. 验收标准

1. 任一合约订单都能回答：
   - 下单前产品规则是什么
   - 下单前风险 gate 结果是什么
   - 使用了什么账户模式、保证金模式和持仓方向
   - submit / ack / fill / cancel 时间线是什么
   - 最终如何影响持仓、保证金、PnL、手续费和资金费

2. 任一时刻都能回答：
   - 当前仓位和方向是什么
   - 当前杠杆和风险暴露是什么
   - 当前距离只减仓或自动停机还有多远
   - 当前对账状态是否允许继续开仓

3. 系统必须自动停机以下情况：
   - 账户快照持续陈旧
   - 私有 WS 或关键 REST 持续失败
   - 持仓、保证金、fill、资金费对账失败
   - 风险限额、保证金率或损失阈值超限
   - 执行状态无法确认

## 8. 建议首批实际开发任务

建议先拆成以下实际开发工作：

1. 合约生产底座与部署约束
2. 合约产品元数据与账户模式建模
3. 合约 pre-trade 风控与限仓
4. 合约执行状态机与恢复硬化
5. 合约持仓、保证金、PnL、资金费账务闭环
6. 合约对账、自动停机与人工恢复
7. 合约观测、报警和控制面收口
8. 合约小资金 `guarded_live` 试运行与放量评审
9. 自动调参二期与 operator 解释收口

首批可直接排期的细化任务见：

- [合约优先版第一批具体开发任务](derivatives_priority_batch1.md)
- [合约优先版第五批具体开发任务（自动调参二期）](derivatives_priority_batch5.md)
- [合约优先版第二批具体开发任务](derivatives_priority_batch2.md)
- [合约优先版第三批具体开发任务](derivatives_priority_batch3.md)
- [合约优先版第四批具体开发任务](derivatives_priority_batch4.md)

当前已经完成到第四批第一轮实现，当前新增的关键能力包括：

- 清算距离与保证金缓冲已能驱动 `only_reduce` 与自动 halt，而不是只做展示
- `guarded_live` 已具备结构化启盘前自检报告
- 小资金试运行已具备统一运行包视图，可直接汇总预检、风险、恢复、trial guard 和 blocker 状态

## 9. 结论

如果当前只允许启动一条实盘升级主线，这条主线就应该是“合约优先版”。  
但合约优先不等于快速放开真钱提交，而是先把保证金、杠杆、持仓、恢复、对账和自动停机全部做成硬门槛，再谈试运行。
