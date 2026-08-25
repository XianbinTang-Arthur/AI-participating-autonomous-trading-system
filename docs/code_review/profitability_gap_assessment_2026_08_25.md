# AATS 从当前状态到真实收益的差距评估与落地路线

> 文档状态：现行代码审查与收益就绪判断
> 最后核对：2026-08-25（当前实现提交 `2c798eab13dedd6c65287d64ae46499d98492ce2`）
> 静态实现基线：`2c798eab13dedd6c65287d64ae46499d98492ce2`
> 模拟运行基线：`derivatives`，deployment generation
> `2c798eab13de-20260825T205326Z-1584-9530`
> 运行证据：`/root/aats/deploy/wsl2-dev/runtime/deployment-evidence/20260825T205451196702Z-derivatives-2c798eab13de.json`
> 禁止外推：本文不构成投资建议，不证明未来收益，不授权 live profile、真实资金或真实订单。

## 1. 结论

当前项目距离“系统能够安全运行”已经不远，但距离“能够以真实资金持续产生净收益”仍然很远。
两者不是同一个完成度。

基于本轮代码、研究 artifact、数据库事件、模拟部署和 Operator 状态的交叉核验，我给出的判断是：

- **系统工程与风控基础完成度约为 80%--88%**：多进程、持久化、风控、恢复、对账、审计、
  研究治理和 Operator 控制面已经形成体系；跨进程 guard 可观测和净仓强平方向错误已修复，
  当前模拟栈可健康运行。剩余差距主要是参数 ACK/readback、故障矩阵和目标平台长稳验证。
- **可信模拟盈利证据成熟度约为 10%--20%**：评估、预注册和数据门禁进一步完善，但三轮共
  10 个可评估唯一候选全部为负收益；模拟盘虽已出现 3 个自然新风险订单、3 个平仓订单和
  28 个 fill 事件，仍远低于校准门，且这些成交没有绑定合格候选。
- **小额真实资金 canary 就绪度低于 10%**：除收益证据为空外，runtime parameter ACK/readback、
  隔离故障矩阵、forward paper 观察和 live 部署入口均未完成；当前 live profile 仍应硬性 NO-GO。
- **“长期稳定真实收益”不能给出可信日历承诺**：在尚无正期望候选、仅 6 个模拟订单样本、
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
| 候选经济性 | 历史 replay 3 个、OHLCV/funding 预注册 4 个、微观结构预注册 3 个候选均为负收益；累计 10/10 失败 | **明确失败** | 淘汰已评估十类表达式；先扩展连续历史和低换手/多持有期研究口径，再注册新机制，不能改阈值追逐同窗结果 |
| 多重检验 | campaign 自动计入 10 次计划、识别 6 个预先重复计划，并执行 bootstrap、Holm、DSR、purged walk-forward | 工具已具备，结果未通过 | 新 campaign 必须继续计入全部尝试和失败项，禁止只汇报赢家 |
| 下单前资金尺度 | 历史 517 个非零目标中 516 个同时触发多个名义额度拒绝；本轮定位为 allocator 只缩审计金额、未缩 qty | 根因已修复并部署；不可覆盖漏斗证据已自动化 | 等待 100 个已成熟自然非零信号，证明同一 decision 的 target notional 不超过现场 cap，且 policy/risk 不因同一尺度问题拒绝 |
| 模拟执行 | 已产生 3 个自然新风险订单和 3 个平仓订单；一次 17 秒重入暴露的重启缓存缺陷已修复，最终部署四个主进程均以 Postgres truth 恢复 15 条历史 fill | **链路已走通；冷静期重启一致性已验证，样本仍不足** | 在同一受控 observation 中累计 ≥100 个成熟非零 target；不得放宽风控凑成交 |
| 成交真实性 | L2 partial/no-fill/队列近似和 paper lifecycle 校准器已有实现；自然订单累计 6 个，仍未绑定合格候选/L2 prediction | 工具可用，证据不足 | 至少 20 个匹配 paper order，并满足生命周期 100%、fill ratio MAE ≤ 0.20、均价误差 ≤ 10 bps、费用误差 ≤ 1 bps、终态 p95 ≤ 5 秒 |
| 已实现净收益 | 已观察到 3 次完整模拟开平仓，但快速重入场曾产生额外 taker fee；这些交易未绑定合格候选，极小样本没有统计意义 | **仍没有可信盈利证据** | 先通过候选门，再形成扣除 fee、funding、slippage 后的冻结 forward paper 净收益序列 |
| 参数生效 | generation schema 与治理状态机已实现；worker ACK/readback 未接入，apply/rollback 返回 501 | 失败关闭是正确行为 | 所有预期 role 完成 prepare/commit/readback，一致读取同一 parameter set ID；失败可确定回滚 |
| 韧性 | 关键任务监督、恢复、对账、kill switch 已较完整；固定故障矩阵 schema 已实现 | 缺现场隔离故障证据 | 在独立 stack/volume 中完成 Redis、NATS、execution restart、stale generation、TTL 五场景，证明无意外新增风险 |
| 前向验证 | 没有合格候选，也没有 paper observation | **0 个有效窗口** | 先通过 paper review，再通过 preapply review；任何 abort、负成本后 edge 或超回撤均淘汰 |
| 真实资金入口 | spot-live、derivatives-live、derivatives-live-monolith 在副作用前失败；future canary `deployable=false` | **明确 NO-GO** | 只有前述证据完整后，另行审计并由人工批准最小权限 canary；本轮不得打开 |

## 4. 三轮候选族的实际结果

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
基差/期限结构和状态标签，而不是继续对这七个表达式做窗口、阈值或符号网格搜索。

### 4.2 微观结构桥接 campaign

提交 `fe6efd65fb283b0d52ec340971de290afed3b490` 将订单簿、主动成交、持仓量和基差接入
Research Factory，并增加按 train/valid/test 分段的输入缺失门。历史 K 线采集器曾把
`confirm=false` 的滚动 bar 推进 checkpoint，导致该时间戳永远不再被重新确认；修复后通过
OKX history-candles 权威刷新，2026-05-16 至 2026-05-28 窗口的 Silver 与 Gold 均为
1,152/1,152 条已收盘，零 K 线缺口、零 funding 缺失。

配置 `microstructure_profit_candidates_v1_20260825` 在结果前固定三种机制和 7.5 bps 成本，
登记证据 SHA-256 为
`a38afb4618b372d88b9c5cea8e9a9ef58cfe875ecbb3e3d125a3637039586019`。提交
`012b91c454b88b0d573a2cfcd0de981c77388f73` 又修复 Python 3.12/3.14 的 `ast.dump`
差异，保证同一 Factor DSL 跨 Windows/WSL2 产生相同签名。

实际 development 结果如下；最大回撤使用绝对值。输入质量全部通过：每个所需微观结构字段
1,150/1,152 非空，缺失率 0.1736%，低于预注册的 1% 上限。

| 预注册假设 | Train 年化净收益 | Train 最大回撤 | Valid 年化净收益 | Valid 成本后 edge | 原始 p 值 | 结论 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 订单簿与成交压力延续 | -9.155649 | 0.165816 | -10.571249 | -3.016909 bps | 1.0 | 淘汰 |
| 成交流与 OI 新仓形成 | -13.985244 | 0.241415 | -12.743515 | -3.636848 bps | 1.0 | 淘汰 |
| mark-mid 基差回归 | -10.880812 | 0.193485 | -8.172298 | -2.332277 bps | 1.0 | 淘汰 |

Campaign 统计证据 SHA-256 为
`ca311e020b3843905b1c6b289bc6d42daafc6825f0e16aac436c4e4e2537bab5`，三次计划全部计入，
`representative_pass_count=0`、`capital_eligible=false`、holdout=`sealed_not_evaluated`。
这证明先前的主要问题确实不是“Factor DSL 没接微观结构数据”；接入后仍无法覆盖成本，当前更
直接的约束是 12 天样本过短、单 bar 持有导致换手成本高，以及尚无冻结候选的 L2 成交校准。

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

最终标准部署后，七个应用容器均 healthy 且 restart count 为 0，`/healthz` 为 200；匿名访问认证端点
`/system/health`、`/system/recovery` 均为预期 401。六个应用没有 ERROR/CRITICAL/traceback；
execution 出现 1 次私有 WebSocket application ping timeout，并在约 5 秒后自动重连，容器未重启，
后续账户刷新、成交同步和自然平仓成功。启动 WARNING 还包含模拟环境 insecure-cookie 声明、一次
OKX system-status 429 后 300 秒退避和一次 stale feature 拒绝；这些外部链路/数据警告仍需持续观察。上一代签名 Operator
快照显示 runtime normal、reconciliation 一致、blocker 0、敞口 0、活动委托 0；这些动态字段
在任何实际操作前仍须重新登录读取。

预算修复的早期部署只产生 flat/0。随后两个部署 generation 各产生 1 个自然新风险订单：
`66be4f5c` 的单链形成 1 个 fill；`2a13eb3b` 的单链形成 1 个订单和 11 个 partial fill。后者
target notional 为 `1250.0000000147308`，只含数量/价格乘积的亚微量化尾差；risk 批准，
allocation/target/policy/risk/plan/intent/order/fill 全阶段存在，未发生尺度型拒绝。

证据工具现场复算同时发现并修复三类审计误判：旧 `RiskDecision` 的 symbol 只存于 event key，
以及启动恢复投影的历史 fill 污染新 decision 观察窗。提交 `2a13eb3b`、`8ff96eb6` 分别补齐
symbol 索引/历史回退和 decision-scoped 查询，并设置仅 `0.000001` quote currency 的量化容差；
没有提高 1,250 风险 cap。修复后最强单链只因成熟非零目标为 1/100 输出 `UNKNOWN`，而不是
`FAIL` 或 `PASS`。最终 `8ff96eb6` 部署后的短窗口尚无新 target，也正确输出 `UNKNOWN`。

### 6.4 平仓冷静期与重启一致性

现场成交时间线证明：系统先把已有空仓完整平掉，约 17 秒后又重新开空；profile 明确配置
`strategy_post_close_cooldown_seconds=300`，因此这不是合理的策略切换。根因是
`FillEventHotCache` 重启后只从 Redis index 恢复；index 缺失或不完整时仍返回列表，导致
Decision Context 看到了平仓 fill，却看不到此前开仓 fill，无法生成 `last_position_closed_at`。

提交 `ad1c68b24d8865e06ad6f57b71ffe22c24ea7e2e` 已实施两层失败关闭：

- 每个运行进程启动时从 Postgres source of truth 读取最近 2,000 条 scoped fill，替换可能不完整的
  Redis 快照；truth load 失败时返回 cache miss，让 Context Builder 回退 Postgres；
- 当前仓位为零且存在明确 `close_only`、`close_long/close_short` 或 close action fill 时，即使对应
  开仓已超出热缓存窗口，也保留最近平仓时间并启动冷静期。

相关回归与完整单元测试已通过。最终标准部署中 gateway、market、decision、execution 四个进程
均记录 `fill_event_cache_bootstrap_truth_reconciled cached_count=15`；decision 的 Redis 启动快照
当时只有 11 条，证明 Postgres 对齐实际补回了生命周期历史。随后 19:59 UTC 的自然决策上下文
恢复出 `last_position_closed_at=19:51:41Z`，新开仓发生在约 444 秒后，已经超过 300 秒门禁。
20:05:53Z 再次自然平仓后，约 2 秒后的上下文又恢复同一平仓锚点，报告 298.12 秒剩余冷静期、
`post_close_cooldown_active`、target=0，且未产生新增风险订单。因此 close anchor 与活动窗口均已获
运行验证；该时点 baseline 本来也未达到入场阈值，尚不能声称观察到“一笔原本应入场的信号被门禁否决”。

### 6.5 模拟执行漏斗证据自动化

提交 `6749ea8a515fc84f8ab8b38de5790c8f5c0fc17c` 新增只读、不可覆盖的漏斗 evaluator/CLI：

- 绑定 deployment evidence 的 SHA-256、commit、generation 与生成时间；
- 按 decision ID 串联 allocation、target、policy、risk、plan、intent、order、fill；
- 只统计经过 settle delay 的自然非零 target，重复事件不能充样本；
- 100 个样本不足为 `UNKNOWN`，超 cap、尺度拒绝或链路矛盾为 `FAIL`；
- 数据库 transaction 强制 read-only，输出不含连接串和原始 payload；
- 无论结果如何都不产生 live 或资金授权。

最强自然非零链证据位于
`/root/aats/deploy/wsl2-dev/runtime/execution-funnel-evidence/2a13eb3ba4d1-20260825T1931Z-v2.json`，
文件 SHA-256 为 `7de9b88872f6089e3b1bb3acce4a870189ba0ae100cd0835fece00eb8fae3b59`，
证据 fingerprint 为 `funnel_040fd87c736593e635b14af10a2d49aee4e6a91decdeb3cd8ee857897ae730be`。
最终部署的最新证据为
`/root/aats/deploy/wsl2-dev/runtime/execution-funnel-evidence/1beba655f321-20260825T2007Z.json`，
SHA-256=`9fe99963d9eedf4cec90fce6fdf4f5565049dc3b62e5465c6423ad5f1da5b179`，
fingerprint=`funnel_8632e0e041c0aa4950508bd0a51268b9e5a58daa75d85055cab6d2078f55a855`；
该窗有 2 个成熟可执行 target（开仓与平仓）、2 个订单和 13 个 fill，无结构/尺度失败，仅因
2/100 输出 `UNKNOWN`。

### 6.6 新候选预注册与完整失败保留

新模块要求 campaign 至少含三个唯一 Factor DSL 签名，严格拒绝未知字段、重复假设、来源 SHA
漂移、非有限成本和重叠持有期。生成阶段无数据库访问；development runner 才读取 Gold，且
任何失败仍保留 return series 并进入完整 campaign。实际 v3 的四个候选全部失败，因此没有
生成 L2 request、没有进入 paper observation，也没有打开 holdout。

### 6.7 微观结构研究桥接与历史修复

提交 `fe6efd65fb283b0d52ec340971de290afed3b490` 完成五个微观结构字段、条件连接、lineage、
字段缺失门和预注册 campaign 的端到端接入，并修复未确认滚动 K 线错误推进 checkpoint。
2026-05-16 至 2026-05-28 的 15m Silver/Gold 已恢复为 1,152/1,152 条已收盘记录；三种新机制
在 7.5 bps 显式成本下仍全部失败，holdout 未打开。提交
`012b91c454b88b0d573a2cfcd0de981c77388f73` 保证 Factor DSL 签名跨 Python 3.12/3.14 稳定。

### 6.8 跨进程 guard 可观测与净空仓风险方向

部署后的签名 Operator 页面曾把净空仓强平距离显示成负值并标为高风险，同时 Gateway 进程读取
不到由 Execution 维护的 trial/derivatives guard，显示“未配置”。提交
`2c798eab13dedd6c65287d64ae46499d98492ce2` 修复两条事实链：净数量为负时按 short 方向计算强平
距离；Gateway 订阅 guard signal 并在 Operator 查询中从 Redis/NATS 缓存回退读取。最终只读 UI
复核显示 trial guard 为“监控中”，最近强平距离为正的 3,081.29%，没有硬阻断。该页面同时显示的
7 个已关闭模拟样本及 24 小时模拟 PnL 不是候选绑定的收益证据，不能用于放行资金。

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

### P1：先扩展研究时域并降低不必要换手（基础桥接已完成，时间样本未完成）

工作：持续采集或权威回填至少 90 天连续 15m 数据（目标 8,640 bars），并为大于 1 bar 的持有期
实现非重叠标签、purge/embargo 和按实际换手扣费，避免当前“每根 bar 翻仓”的成本结构主导结果。
只有数据与口径冻结后，才先写经济假设卡、再写参数，注册新的机制族；继续把失败、无数据和重复
项计入 trial count。

通过条件：

- 连续 15m 历史至少 8,640 bars，时间缺口为 0，所需字段在每段不超过预注册缺失阈值；
- 多持有期收益没有重叠标签泄漏，成本由实际持仓变化而非每 bar 固定扣减；
- development 至少满足 `real_factor_development`：总 500、train 300、valid 100、sealed test 100；
- 候选净年化收益 > 0、成本后 edge > 0、最大回撤 ≤ 20%；
- purged walk-forward、bootstrap lower bound、Holm 和 DSR 全部通过；
- 不通过的候选淘汰，不读取 holdout，不修改阈值追逐结果。

本轮 OHLCV/funding 四个和微观结构三个预注册机制都满足“先写卡再运行”，但通过数为 0。下一轮
不得把这些表达式改窗口后重新包装为新机制，也不能把 12 天结果当成 90 天门已完成；应先完成
连续时域和低换手口径，再注册新的 trial family。

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
candidate_statistical_edge = FAIL / 10 of 10 unique candidates rejected
paper_execution_calibration = UNKNOWN / 6 natural orders, below 20 and no L2 binding
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
