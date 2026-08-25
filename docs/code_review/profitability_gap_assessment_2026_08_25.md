# AATS 从当前状态到真实收益的差距评估与落地路线

> 文档状态：现行代码审查与收益就绪判断
> 最后核对：2026-08-25 19:17 UTC
> 静态实现基线：Git `66be4f5c4fbb180e2a286ff7b6d3844b3064ea9f`
> 模拟运行基线：`derivatives`，deployment generation
> `66be4f5c4fbb-20260825T191234Z-375-2537`
> 运行证据：`/root/aats/deploy/wsl2-dev/runtime/deployment-evidence/20260825T191403351222Z-derivatives-66be4f5c4fbb.json`
> 禁止外推：本文不构成投资建议，不证明未来收益，不授权 live profile、真实资金或真实订单。

## 1. 结论

当前项目距离“系统能够安全运行”已经不远，但距离“能够以真实资金持续产生净收益”仍然很远。
两者不是同一个完成度。

基于本轮代码、研究 artifact、数据库事件、模拟部署和 Operator 状态的交叉核验，我给出的判断是：

- **系统工程与风控基础完成度约为 75%--85%**：多进程、持久化、风控、恢复、对账、审计、
  研究治理和 Operator 控制面已经形成体系；当前模拟栈可健康运行。
- **可信模拟盈利证据成熟度约为 10%--20%**：评估、预注册和数据门禁进一步完善，但累计两轮
  7 个可评估唯一候选全部为负收益，且模拟盘尚无可用于成交模型校准的订单/成交样本。
- **小额真实资金 canary 就绪度低于 10%**：除收益证据为空外，runtime parameter ACK/readback、
  隔离故障矩阵、forward paper 观察和 live 部署入口均未完成；当前 live profile 仍应硬性 NO-GO。
- **“长期稳定真实收益”不能给出可信日历承诺**：在尚无正期望候选、零模拟成交校准样本、
  零前向盈利窗口时，用“两周上线”或“完成 80%”描述都会制造虚假确定性。

换成最直接的业务语言：**当前不是“已有赚钱机器，只差打开实盘开关”，而是“安全、治理和研究
基础已经搭好，但尚未找到经过统计与成交成本验证的赚钱策略”。** 当前第一约束是经济有效性，
第二约束才是执行与上线工程。

## 2. 事实边界

本文严格区分三类事实：

1. **静态实现事实**：当前 Git 基线中的代码、配置、migration 和测试；
2. **一次性运行快照**：2026-08-25 本地 derivatives 模拟栈的容器、事件和 UI 状态；
3. **仍未知事项**：未来信号、成交、成本、PnL、交易所异常下行为和真实资金结果。

一次部署健康不能证明下一小时健康；一段回测不能证明未来收益；模拟账户余额、funding 投影或
模型中的 expected edge 也不能替代已结算交易净收益。

## 3. 收益链逐层审计

| 收益链层级 | 当前事实 | 判断 | 到下一门禁的实际差距 |
| --- | --- | --- | --- |
| 市场数据接入 | BBO、books5、trades、OI、liquidations 已进入 derivatives 模拟拓扑；曾有单个 15 分钟窗口通过完整性与 lineage 门 | 局部可用，不代表长区间连续可用 | 为每个研究/回放区间生成无缺口 eligibility manifest，并持续验证 collector freshness |
| 研究数据与协议 | development/valid/封存 holdout 分段、artifact fingerprint、真实试验族计数已实现 | 研究纪律基本成形 | 候选必须升级到 `paper_review`/`preapply_review` 的更高样本与成本证据门 |
| 候选经济性 | 历史 replay 的 3 个代表候选与新预注册的 4 个唯一候选均为负收益；累计通过数 0 | **明确失败** | 淘汰已评估七类表达式；下一轮必须来自新增数据域或更强经济机制，不能继续围绕同一 OHLCV/funding DSL 做参数寻优 |
| 多重检验 | campaign 自动计入 10 次计划、识别 6 个预先重复计划，并执行 bootstrap、Holm、DSR、purged walk-forward | 工具已具备，结果未通过 | 新 campaign 必须继续计入全部尝试和失败项，禁止只汇报赢家 |
| 下单前资金尺度 | 历史 517 个非零目标中 516 个同时触发多个名义额度拒绝；本轮定位为 allocator 只缩审计金额、未缩 qty | 根因已修复并部署；不可覆盖漏斗证据已自动化 | 等待 100 个已成熟自然非零信号，证明同一 decision 的 target notional 不超过现场 cap，且 policy/risk 不因同一尺度问题拒绝 |
| 模拟执行 | 部署后 10 个新 target 均为 flat/0；risk 均批准，但没有 plan、order intent、order 或 fill | **尚未形成运行证据** | 收集真实自然信号产生的完整 paper 生命周期；不得人工伪造业务成功或放宽风控凑成交 |
| 成交真实性 | L2 partial/no-fill/队列近似和 paper lifecycle 校准器已有实现 | 工具可用，样本为零 | 至少 20 个匹配 paper order，并满足生命周期 100%、fill ratio MAE ≤ 0.20、均价误差 ≤ 10 bps、费用误差 ≤ 1 bps、终态 p95 ≤ 5 秒 |
| 已实现净收益 | 当前执行订单总量只有一条历史 BLOCKED 记录，成交与已实现交易 PnL 均为 0 | **没有盈利证据** | 先获得可校准模拟成交，再形成扣除 fee、funding、slippage 后的 forward paper 净收益序列 |
| 参数生效 | generation schema 与治理状态机已实现；worker ACK/readback 未接入，apply/rollback 返回 501 | 失败关闭是正确行为 | 所有预期 role 完成 prepare/commit/readback，一致读取同一 parameter set ID；失败可确定回滚 |
| 韧性 | 关键任务监督、恢复、对账、kill switch 已较完整；固定故障矩阵 schema 已实现 | 缺现场隔离故障证据 | 在独立 stack/volume 中完成 Redis、NATS、execution restart、stale generation、TTL 五场景，证明无意外新增风险 |
| 前向验证 | 没有合格候选，也没有 paper observation | **0 个有效窗口** | 先通过 paper review，再通过 preapply review；任何 abort、负成本后 edge 或超回撤均淘汰 |
| 真实资金入口 | spot-live、derivatives-live、derivatives-live-monolith 在副作用前失败；future canary `deployable=false` | **明确 NO-GO** | 只有前述证据完整后，另行审计并由人工批准最小权限 canary；本轮不得打开 |

## 4. 两轮候选族的实际结果

本轮不是根据旧文档推断，而是在 WSL2 的真实 Gold development 数据上执行 development batch，
再运行 2,000 次 deterministic block bootstrap（seed 7）。holdout 保持
`sealed_not_evaluated`。

| 唯一假设 | Train 年化净收益 | Train 最大回撤 | Valid 年化净收益 | Valid 成本后 edge | 原始 p 值 | 结论 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| funding drift | -2.143776 | 0.301318 | -0.484981 | -0.553631 bps | 0.618191 | 淘汰 |
| volatility filter | -2.441648 | 0.380434 | -1.250580 | -1.427603 bps | 0.749125 | 淘汰 |
| range pressure | -2.486702 | 0.386519 | -1.394670 | -1.592089 bps | 0.777111 | 淘汰 |

Campaign 的完整口径为：

- 计划数/试验数：10；
- 预先识别的唯一假设：4；预先重复计划：6；
- 有 development return series 的代表候选：3；
- 代表候选通过数：0；
- 共同失败门：aggregate OOS return、bootstrap lower bound、bootstrap alpha、Holm、
  deflated Sharpe、positive fold ratio；
- `capital_eligible=false`；holdout 未打开。

这组结果的正确动作不是降低门槛，也不是打开 holdout 看能否“翻盘”，而是停止在这三类假设上
继续消耗多重检验预算，回到经济机制和特征生成阶段。

### 4.1 新经济假设预注册 v3 campaign

提交 `410e3a40c910f07f0722704a25cf14e1fb376c91` 新增严格预注册入口。在任何 development
结果产生前，配置固定四个不同机制、可证伪条件、容量假设、Factor DSL、同一 Gold 窗口、
1 bar 持有期和 fee/slippage/funding 成本。四个 factor signature 均不同，且 funding 成本进入
plan、真实 experiment 与 hypothesis fingerprint；holdout 未读取。

实际结果如下。最大回撤列使用绝对值：

| 预注册假设 | Train 年化净收益 | Train 最大回撤 | Valid 年化净收益 | Valid 成本后 edge | 原始 p 值 | 结论 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 成交量确认动量 | -1.509237 | 0.269958 | -1.293727 | -1.476857 bps | 0.852574 | 淘汰 |
| 前一日区间突破 | -0.047237 | 0.057279 | -0.178736 | -0.204037 bps | 0.695152 | 淘汰 |
| 短周期反转 | -2.670243 | 0.381328 | -2.370211 | -2.705720 bps | 0.970515 | 淘汰 |
| funding 拥挤反转 | -0.945619 | 0.240233 | -0.721901 | -0.824088 bps | 0.738631 | 淘汰 |

完整 campaign 计入 4 个计划和 4 个唯一假设，代表通过数为 0，
`capital_eligible=false`，holdout=`sealed_not_evaluated`。Evidence 位于
`/root/aats/artifacts/research/research_factory/campaigns/profit_candidates_v3_20260825/campaign_evidence.json`，
SHA-256=`a67403ace4b6197005f161ce1b88aaf42f4231341afa00ab0f2d2966f84d968a`。

这次结果进一步缩小了不确定性：**问题不只是旧候选实现过时；在同一 OHLCV/funding 研究域内，
四个预先固定的新机制同样无法覆盖显式成本。** 下一轮高价值工作应优先扩充可审计的 L2/订单流、
基差/期限结构和状态标签，而不是继续对现有七个表达式做窗口、阈值或符号网格搜索。

## 5. 为什么 expected edge 和系统健康仍不能推导收益

历史非零目标曾记录约 5.03--32.30 bps 的 `expected_net_edge_bps`。它是模型在决策时的预期输入，
不是成交后事实，原因至少包括：

1. 这些目标没有穿过风险门，没有订单和成交；
2. 没有真实 fill price，无法计算滑点；
3. 没有成交费用与 funding 的逐笔结算闭环；
4. 没有 adverse selection、partial/no-fill 和队列位置的现场分布；
5. 没有 forward sample，无法判断信号是否在部署后的市场条件下继续成立。

同样，容器 healthy、reconciliation normal、blocker=0 证明系统当前可以继续观察，不证明它应该
承担资金风险。工程门解决的是“不要因软件错误亏钱”；收益门解决的是“策略是否真的有正期望”。

## 6. 已经落地的工作

### 6.1 候选收益证据自动化

提交 `d026bc19455f2e6a21e0695b5e98294d930db9dc` 已完成：

- development 实验自动写出与 metrics 同源的 train/valid 净收益序列；
- holdout artifact 只保留封存状态和 fingerprint，不暴露收益；
- campaign 自动核对计划、实验、数据集、协议和 SHA；
- 全试验计数、重复假设分组、block bootstrap、Holm、DSR 和 walk-forward 自动生成；
- 输出不可覆盖，且不具备参数写入、订单或 live 授权。

### 6.2 模拟执行预算一致性

提交 `0762a4aeed87075b9001717383b9565416c7271b` 已完成：

- 修复无显式 legs 的方向 intent 只缩预算、不缩 qty 的问题；
- 衍生品 margin budget 按 `margin × leverage` 转换为 notional capacity；
- 方向策略从所有现有正值额度中取最严格单步 cap，当前模拟配置为 1,250；
- 减仓与独立全平不被新增风险预算错误截断；
- 未提高任何风险限额，未改变 kill switch、recovery 或 live gate。

### 6.3 当前部署观察

最新标准部署后，七个应用容器均 healthy，`/healthz` 为 200；匿名访问认证端点
`/system/health`、`/system/recovery` 均为预期 401。七个应用日志未匹配到 error-level、fatal、
critical、exception 或 traceback，正常 flat/0 决策也不再产生 WARNING。上一代签名 Operator
快照显示 runtime normal、reconciliation 一致、blocker 0、敞口 0、活动委托 0；这些动态字段
在任何实际操作前仍须重新登录读取。

预算修复的前两代部署观察分别产生了 25 组和 6 组 target/risk；risk 均批准，但目标均为
flat/0，因此没有形成 plan/order/fill。最新部署已运行新的漏斗证据 CLI；当前 artifact 绑定
`66be4f5c` deployment evidence 并覆盖 5 个自然 flat/0 决策周期，结果为 `UNKNOWN`：成熟
自然非零目标 0、订单 0、成交 0，
`production_ready=false`、`trading_ready=false`。结论是：**修复已通过确定性测试并成功部署，
但自然非零信号下的运行验收仍为 UNKNOWN，而不是 PASS。**

### 6.4 模拟执行漏斗证据自动化

提交 `6749ea8a515fc84f8ab8b38de5790c8f5c0fc17c` 新增只读、不可覆盖的漏斗 evaluator/CLI：

- 绑定 deployment evidence 的 SHA-256、commit、generation 与生成时间；
- 按 decision ID 串联 allocation、target、policy、risk、plan、intent、order、fill；
- 只统计经过 settle delay 的自然非零 target，重复事件不能充样本；
- 100 个样本不足为 `UNKNOWN`，超 cap、尺度拒绝或链路矛盾为 `FAIL`；
- 数据库 transaction 强制 read-only，输出不含连接串和原始 payload；
- 无论结果如何都不产生 live 或资金授权。

当前现场证据位于
`/root/aats/deploy/wsl2-dev/runtime/execution-funnel-evidence/66be4f5c4fbb-20260825T1917Z.json`，
文件 SHA-256 为 `9fcdb540256dc2c9e555b4fbc1d1e667909f105e8358be6488b6fe2cd9391eee`，
证据 fingerprint 为 `funnel_742ac5d16e2c933d00c5e0ca423c0729bdd6e5a31e4455fb59a4e89f8b0849f3`。

### 6.5 新候选预注册与完整失败保留

新模块要求 campaign 至少含三个唯一 Factor DSL 签名，严格拒绝未知字段、重复假设、来源 SHA
漂移、非有限成本和重叠持有期。生成阶段无数据库访问；development runner 才读取 Gold，且
任何失败仍保留 return series 并进入完整 campaign。实际 v3 的四个候选全部失败，因此没有
生成 L2 request、没有进入 paper observation，也没有打开 holdout。

## 7. 后续可落地工作包与硬验收门

以下顺序是依赖关系，不应并行跳过前置门。

### P0：关闭模拟执行漏斗（证据工具已完成，样本积累进行中）

工作：持续只读观察自然非零 target；现有 CLI 已按 decision ID 串联 allocation、target、policy、
risk、plan、intent、order、fill，并按部署 generation 输出数量、notional、拒绝原因和链路完整性。
后续只需以新的不可覆盖输出周期复跑，不能修改旧 artifact。

通过条件：

- 至少 100 个连续自然非零目标不存在“只因上游尺度超过已知 cap”而拒绝；
- 空仓新增风险的 target notional ≤ 当前最严格 cap；
- plan/order/fill 必须来自系统自然信号，历史 BLOCKED 不算样本；
- 风控拒绝必须保留且可解释，绝不为了提高通过率放宽上限。

### P1：建立下一轮真正不同的候选族（本轮已完成并全数失败）

工作：先写经济假设卡，再写参数。候选应覆盖至少三种不同机制，例如资金费/基差回归、流动性与
订单流失衡、波动状态切换；每种机制明确持有周期、失败条件、成本来源和可交易容量。每个计划在
运行前固定，并继续把失败、无数据和重复项计入 trial count。

通过条件：

- development 至少满足 `real_factor_development`：总 500、train 300、valid 100、sealed test 100；
- 候选净年化收益 > 0、成本后 edge > 0、最大回撤 ≤ 20%；
- purged walk-forward、bootstrap lower bound、Holm 和 DSR 全部通过；
- 不通过的候选淘汰，不读取 holdout，不修改阈值追逐结果。

本轮四个预注册机制已经满足“先写卡再运行”，但通过数为 0。下一轮不得把这四个表达式改窗口
后重新包装为新机制；应先建设当前 Gold bars 缺失的 L2/订单流、基差/期限结构或可审计状态标签，
再注册新的 trial family。

### P2：L2 成本证据与 paper 生命周期校准

工作：为通过 P1 的候选生成绑定 plan/dataset/window 的 L2 request，覆盖完整 eligibility 窗口；
在模拟盘收集对应 paper order，并用现有校准器比较 partial/no-fill、均价、费用和终态时延。

通过条件采用当前代码契约：

- 匹配订单数 ≥ 20，所有观察订单有 L2 prediction；
- 生命周期有效率 100%；
- fill ratio MAE ≤ 0.20；
- 平均价格误差 ≤ 10 bps，平均费用误差 ≤ 1 bps；
- command-to-terminal p95 ≤ 5,000 ms。

### P3：Forward paper review

工作：冻结候选、参数、成本模型和代码指纹，不再回看后调参；在连续模拟运行中按既有
`paper_review` 观察门自动生成净收益、回撤、fillable、partial fill 和 metric drift 证据。

通过条件采用当前代码契约：

- 数据总量 ≥ 1,000 bars，train/valid/test 至少 600/200/200；
- 观察 ≥ 96 bars、≥ 20 个事件；
- fillable ≥ 0.85、partial fill ≤ 0.15；
- 成本后平均 edge > 0.2 bps、drawdown ≤ 0.15、metric drift ≤ 0.35；
- 没有 abort，且统计门和 L2 校准持续有效。

### P4：参数代次与故障恢复闭环

工作：接入 decision/execution 等预期 role 的 prepare、commit、readback；实现独立命名、独立
volume 的故障注入 harness；用自动 artifact 证明五个固定故障场景。

通过条件：

- 同 generation、payload SHA、parameter set ID 在所有 role 精确一致；
- 超时、stale、部分 ACK 只能进入 FAILED/ROLLBACK_REQUIRED；
- 五个故障场景都证明新增风险被阻断、无意外订单、恢复对账正常且清理完成。

### P5：Pre-apply 与一次性 holdout

只有 P0--P4 全部通过才允许进入。先完成 `preapply_review`：总 2,000 bars、train/valid/test
至少 1,200/400/400；观察 ≥ 192 bars、≥ 40 事件，fillable ≥ 0.90、partial fill ≤ 0.10、
成本后 edge > 0.5 bps、drawdown ≤ 0.10、drift ≤ 0.25。之后才可 claim 一次性 holdout。

holdout 失败即淘汰，不得第二次读取，不得更换 actor 重试，不得用 test 继续调参。

### P6：未来最小真实资金 canary

这不是本轮授权事项。只有 P5 通过、live 部署代码经过独立安全审计且人工明确批准后，才可评估
当前 `deployable=false` 的 canary 契约。初始上限继续是单 BTC 永续、逐仓 1x、单笔 25 USDT、
总敞口 50 USDT、日损失 5 USDT、禁止提现/划转、双人签、人工恢复。任何一项缺失继续 NO-GO。

## 8. 资源排序

建议未来工作量按以下比例投入：

- 45%：新经济假设、数据质量和严格 development 研究；
- 25%：模拟订单样本、L2 回放和成本校准；
- 15%：forward paper 观察与归因；
- 10%：参数 ACK/readback 与故障矩阵；
- 5%：UI、文档和非阻断性工程整理。

在没有正候选时继续大量优化 UI、扩展 AI 代理或打开 live profile，对真实收益的边际贡献接近零，
并可能掩盖主要问题。研究失败应作为高价值结果保留，因为它阻止系统用真实资金验证一个已知
负期望假设。

## 9. 最终上线判定

截至本文快照，正式判定为：

```text
simulation_runtime_operational = PASS (snapshot only)
candidate_statistical_edge = FAIL
paper_execution_calibration = UNKNOWN / no samples
forward_paper_profitability = UNKNOWN / no eligible candidate
parameter_runtime_readback = UNKNOWN
fault_matrix = UNKNOWN
holdout = SEALED / correctly not accessed
production_ready = false
trading_ready = false
live_funds_authorized = false
```

最短的正确路径不是“尽快发一笔真实订单”，而是依次取得：**正的开发段统计证据 → 可信成交成本
→ 冻结后的前向模拟净收益 → 参数与故障恢复闭环 → 一次性 holdout → 独立批准的最小 canary**。
任何跨级都会把尚未回答的研究问题转化为真实资金风险。
