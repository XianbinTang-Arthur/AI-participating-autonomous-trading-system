# 模拟盘执行漏斗预算一致性整改 SOW

> 文档状态：已实施、待 100 个自然非零目标运行验收的任务书
> 最后核对：2026-08-25（起始 HEAD `d026bc19`；预算实现 `0762a4aeed87075b9001717383b9565416c7271b`；漏斗证据实现 `6749ea8a515fc84f8ab8b38de5790c8f5c0fc17c`；现场纠偏 `2a13eb3ba4d16e0b7391bf874b00d90a227ea726`、`8ff96eb6530fb2cc5768fcb3398b8212b3b86e06`、`ad1c68b24d8865e06ad6f57b71ffe22c24ea7e2e`）
> 核对范围：方向策略 sleeve、Portfolio Allocator v2、衍生品风险额度及 derivatives 模拟盘  
> 运行时边界：只修复本地 derivatives 模拟栈的预算一致性；不提高风险上限，不启动 live profile，
> 不接触真实资金，也不把模拟成交解释为盈利证明。

## 1. 业务目标与边界

当前模拟盘已产生非零方向目标，但组合分配器虽然把批准预算压到 5,000，最终目标数量仍未
按比例缩放，导致约 46,000–121,000 的名义目标进入风险引擎，并被现有 2,500 单标的上限、
1,250 待成交上限和 5,000 总敞口上限全部拒绝。目标是让上游实际输出与现有风险配置同量纲、
同额度，使模拟盘能够在不放宽风控的前提下验证订单、成交、费用和 PnL 链路。

非目标包括提高任何风险额度、绕过 only-reduce/kill switch/recovery 门禁、修改策略信号、
打开 holdout、发布参数或进行真实资金试单。

## 2. 模块职责与领域模型

`StrategyCoordinatorService` 负责把方向策略的理论目标转换为 sleeve 预算；
`PortfolioAllocatorV2Phase2` 负责按预算缩放 intent/legs、执行组合预算再分配并形成最终目标；
`RiskEngine` 保持最终否决权。`SleeveBudgetProfile`、`SleeveBudgetAssignment`、
`AllocatorBudgetSnapshot` 和 `PortfolioAllocationDecision` 是整改的审计对象。

## 3. 输入与输出接口

输入是现有 `PositionTarget`、方向 `StrategySleeveIntent`、运行设置中的名义/保证金/待成交额度，
以及目标杠杆。输出仍是原有 `PortfolioAllocationDecision` 和最终 `PositionTarget`，不改变公开
schema。输出必须满足 `approved_delta_qty × reference_price` 与 `approved_notional` 一致，允许
Decimal 量化误差但不得出现数量未缩放、金额已缩放的分裂状态。

运行验收另新增只读 CLI：输入标准 deployment evidence、观察结束时间、现场新风险 cap 和主交易
数据库环境变量名；输出不可覆盖的执行漏斗 JSON。它只保存聚合计数、decision ID、数值、阶段
存在性和原因码，不保存原始 payload 或数据库连接信息。

## 4. 数据库 Schema、表、索引与约束

不修改数据库 schema、migration、索引或约束。现有 `event_store` 中的 allocation、target、
policy、risk、execution、order 和 fill 事件用于只读验收；历史错误事件保留为审计证据，不回写。

## 5. 事务、一致性与并发

整改不新增跨服务事务。一次 allocator 调用内，intent、execution legs、budget snapshot 和
allocation target 必须由同一预算比例派生。两级缩放（sleeve 上限、组合上限）必须依次作用于
上一级已缩放结果，不能只改审计金额而遗漏数量。并发下继续由现有事件 ID 和仓储事务控制。

## 6. 授权、认证与数据安全

实现与单测不读取 `.env`。运行验收只通过受管 profile 注入既有配置，不显示连接串、密码、
token 或 API key。只部署 derivatives 模拟 profile；live profile 保持失败关闭。

## 7. 错误处理与幂等

无有效价格时继续失败关闭到现有下游风险校验，不猜测数量。额度为 0 表示该维度未配置，不能
被误解释为零额度。比例限制在 `[0, 1]`；重复计算对相同输入产生相同 Decimal 结果。任何预算
或风险异常不得回退到未缩放原始目标。

## 8. 状态转换与生命周期

```text
THEORETICAL_TARGET
  -> SLEEVE_BUDGET_CAPPED
  -> PORTFOLIO_BUDGET_CAPPED
  -> POLICY_EVALUATED
  -> RISK_APPROVED | RISK_REJECTED
  -> PAPER_ORDER_INTENT -> PAPER_FILL
```

本任务只保证前两次 cap 真正作用于数量；后续状态仍必须由实际运行证据确认。风险拒绝不允许
伪造为订单或成交。

## 9. 缓存与性能

新增逻辑只对少量 Decimal 配置求最小值并进行常数次乘除，不增加数据库查询、网络请求或缓存。
不得为预算计算扫描历史事件。

## 10. 日志、监控与审计

继续使用 `AllocatorBudgetSnapshot` 记录 requested/approved notional、requested/approved delta、
cap 来源和原因码。运行验收对比 allocation、position target、risk、execution、order、fill 的
同一 decision ID，并记录事件数量与拒绝原因；不输出完整环境变量。

漏斗证据必须区分 `PASS`、`FAIL`、`UNKNOWN`：少于 100 个已成熟自然非零 target 只能是
`UNKNOWN`；超 cap、纯尺度风险拒绝、阶段断链、风险拒绝后仍有订单或孤儿成交属于 `FAIL`。

## 11. 测试策略

1. 单测证明无显式 legs 的方向 intent 会按预算比例缩放数量；
2. 单测证明衍生品 margin budget 乘目标杠杆后再与 notional 比较；
3. 单测证明方向策略的单步目标不超过现有待成交/敞口额度；
4. 回归证明平仓和减仓不因新增风险预算而被截断；
5. 运行 Ruff、相关单测、完整 unit，并在 WSL2 derivatives 模拟栈复测执行漏斗。
6. 漏斗 evaluator 单测覆盖无信号、100 条完整链、超 cap、尺度拒绝、缺阶段、拒绝后订单和
   deployment identity 失败关闭；CLI 在已部署容器内使用 read-only transaction 生成现场证据。
7. 重启后 Fill 热缓存必须从 Postgres truth hydrate；truth loader 失败时 Context Builder 回退 PG；
   仅保留明确平仓 fill 时仍能生成 post-close cooldown。

## 12. Migration、Rollback 与兼容

无 migration，无 schema/API 变化。回滚可恢复 allocator 缩放与 coordinator budget profile 的
本次差异；历史事件无需删除。旧消费者继续读取相同字段，只会看到数值恢复一致。

## 13. 配置与环境隔离

不新增配置。方向衍生品单步 cap 从现有正值额度取最小值：`max_notional_per_symbol`、
`max_gross_notional_per_symbol`、`max_pending_notional_per_symbol`、`max_total_open_notional`，以及
显式非零的 long/short/gross/net 风险额度。simulation 与 live 使用同一防御性算法，但本次只在
simulation 验收，且不因此解除 live 门禁。

## 14. 代码组织与依赖

预算来源解析保留在 `coordinator.py`，比例应用和量纲换算保留在 `allocator.py`；测试追加到现有
`test_strategy_coordinator.py`。不引入第三方依赖，不把最终风险逻辑复制到决策引擎。

## 15. 文档与运维手册

完成后更新收益就绪 runbook、验收记录和正式差距评估，明确区分代码修复、模拟运行证据、
尚未发生的成交/PnL 事实和盈利未知项。若模拟信号窗口内没有新信号，必须记录为未验证而非通过。
漏斗 artifact 的命令、字段、退出码和不可覆盖语义必须登记到现行 runbook。

## 16. 部署与验收标准

- 无 legs intent 的两级预算缩放同时更新 qty 和 notional；
- derivatives margin budget 以 `margin × leverage` 转成 notional capacity；
- 当前 profile 的空仓方向目标不超过 1,250 名义金额；
- 减仓/平仓不受新增风险额度缩放；
- 风险上限、kill switch、recovery 和 live gate 未放宽；
- Ruff、完整 unit 和最窄相关测试通过；
- 标准部署脚本完成 derivatives 模拟部署，容器健康；
- 实际订单/成交只按观测结果陈述，若仍被其他门禁阻塞则准确列出原因。
- 漏斗 evidence 绑定标准 deployment evidence 的 profile、deployed commit、generation、生成时间
  和 SHA-256；缺少自然非零样本时退出 `2` 并写 `UNKNOWN`，绝不写伪 PASS。

### 实施结果

预算与漏斗代码、单元回归和 derivatives 标准部署已完成。早期 flat/0 窗口之后，两个 generation 各产生
1 个自然新风险订单；两者之间另有 1 个自然平仓订单。最强单链的 allocation/target/policy/risk/plan/intent/order/fill 全部存在，
1 个订单产生 11 个 partial fill，risk 批准且无尺度型拒绝。现场复算还修复了旧 RiskDecision
symbol 索引缺失、启动恢复 fill 污染和亚微量化尾差误判；未提高风险 cap。

平仓后约 17 秒重入场又暴露 Fill 热缓存重启后历史不完整会漏掉 close anchor。`ad1c68b2` 已补
Postgres truth hydrate、失败回退和显式 close fill 锚点；完整单元回归为
`4577 passed, 30 skipped, 94 subtests passed`。最终标准部署 `1beba655` 的四个主进程均从
Postgres 恢复 15 条 fill；最新自然决策恢复出真实平仓锚点，并在 300 秒门禁到期后才重新开仓。

当前执行链实现验证和服务部署验证为 PASS，但统计门要求 100 个成熟非零目标，现只有 1，故
漏斗运行验收仍为 UNKNOWN；累计 5 个自然订单也未绑定合格候选，不得写成“已证明模拟盈利”。详见
[`../code_review/profitability_gap_assessment_2026_08_25.md`](../code_review/profitability_gap_assessment_2026_08_25.md)。
