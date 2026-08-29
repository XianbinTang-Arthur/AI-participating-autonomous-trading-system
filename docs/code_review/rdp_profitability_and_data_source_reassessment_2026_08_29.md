# RDP 收益成熟度与数据价值链复评

> 文档状态：现行代码审查与决策依据
> 核对日期：2026-08-29
> 静态核对起始基线：`main@3c8d3c42`
> 运行证据采样：2026-08-29 16:23:17 `Asia/Shanghai`；只对该采样时点成立
> 范围：AATS RDP 数据采集、加工、研究、回测、成本、归因及独立 Research OS 来源合同
> 边界：只读核验；未访问私有账户、未读取凭证、未启动 live、未下单；运行快照会随时间失效

## 1. 执行结论

RDP 当前不是“再补一点防御就能盈利”，而是**已经积累了数据资产，却还没有把这些资产转化为
真实成本后、独立样本外、可前向复现的正 Alpha**。现有三个历史/研究 campaign 合计 10 个唯一
候选均未通过；其中“成本后”指各 campaign 当时显式使用的 fee、funding、slippage 等代理成本，
不是足量真实 paper fills 校准后的最终成本。最近三个微观结构候选也全部失败。

因此当前收益状态是 `NO-GO`。减少必要的防泄漏或成本门不会创造 edge，只会把噪声伪装成收益。

为避免虚假精度，本复评不用单一百分制，而采用证据等级：

| 能力 | 当前等级 | 可复核事实 |
| --- | --- | --- |
| 数据资产 | `AMBER / 已形成但集中` | OKX K 线、funding、trades、BBO/books5、OI、mark、long/short、liquidation 和历史 L2/trades 有真实数据 |
| 连续性与多样性 | `RED-AMBER` | 主要是单场所、BTC 微观数据；采样时七个应用容器均停止，实时数据不再前进 |
| 加工与特征消费 | `AMBER` | 五类微观 Silver 已有数据，但正式 feature 只消费其中三类；强平、volume profile、历史 L2/trades 没有被充分研究 |
| 研究统计纪律 | `AMBER-GREEN` | 有预注册、train/valid/sealed test、purged walk-forward、bootstrap、Holm 和 DSR |
| Alpha 证据 | `RED` | 没有通过成本代理与独立样本外门、可进入资本使用的候选 |
| 真实执行证据 | `RED` | 有成本/L2 replay 代码，但足量 paper fill、队列、容量及 replay-paper 偏差证据未建立 |
| 宏观 regime | `RED / 未实现` | 没有现行 schema、collector、point-in-time/vintage join、feature 或研究消费者 |
| 生产盈利就绪 | `RED / 0 个合格候选` | 无合格 Alpha、无完整 forward paper 证据；live 仍为 NO-GO |

## 2. 当前真实数据覆盖

下表来自附录 A 的只读 SQL。所有时间均为数据库 `Asia/Shanghai` 显示值；行数只代表表中记录，
不等于无缺口覆盖，也不等于可用于资本决策。

| 数据域 | 当前数据库事实 | Alpha/成本用途 | 实质缺口 |
| --- | --- | --- | --- |
| Swap K 线 | staging 15m `45,476` 行，2025-12-12 15:00 至 2026-08-29 11:30；Silver 1h `16,746` 行，自 2024-10-01；BTC/ETH | 标签、趋势/反转、波动、量价基准 | 1m/5m 不是默认连续链路；Gold 有滞后 |
| Spot K 线 | staging 15m `26,488` 行，2026-03-17 10:00 至 2026-08-29 11:30；Silver 1h `16,746` 行，自 2024-10-01；BTC/ETH | 现货确认、basis 与 beta | 仅 K 线；没有现货 L2/trades 研究链 |
| Funding | staging `1,447` 行；Silver `1,354` 行，2026-01-15 至 2026-08-28 08:00；BTC/ETH | carry、拥挤、基差、净收益 | 单一场所；funding time 是结算语义，不能冒充 collector 新鲜度 |
| Trades | `4,679,610` 行，2026-08-24 10:47:54.051 至 2026-08-29 11:31:41.243；BTC swap | order flow、VWAP、冲击、成交回放 | 前瞻窗口不足五个整日，当前停采 |
| BBO / Books5 | `257,586` / `459,260` 行，2026-08-24 10:47:54 至 2026-08-29 11:31:43；BTC swap | spread、imbalance、top5 depth、fill feasibility | 采样 top5 不是逐事件完整订单簿，不能恢复撤单或真实队列位置 |
| 官方历史 L2 | `161,551,006` 行，2026-07-22 08:00:00.006 至 2026-08-21 07:59:59.975；关系大小约 `138 GB` | 深度因子和 L2 replay | 单一 BTC swap、30 天；覆盖 regime 不足 |
| 官方历史 Trades | `52,585,844` 行，2026-07-22 08:00:00.046 至 2026-08-24 23:59:59.983；关系大小约 `26 GB` | 事件成交回放 | 表范围大于 30 日 campaign，不能把全部记录都归属于同一 manifest |
| OI / Mark | OI 1h `2,764` 行，2026-02-19 至 2026-08-29 11:00；Mark 1m `106,403` 行，2026-03-21 至 2026-08-29 11:18；BTC swap | 杠杆状态、price-OI、basis | 单标的、历史短 |
| Long/Short | 5m `14,299` 行；1h `3,024` 行；BTC swap | 拥挤极值与 regime | 单场所用户结构偏差，不能当作全市场仓位 |
| Liquidations | raw `470,541` 行，2026-04-19 12:39:25.243 至 2026-08-29 11:31:45.472；来源含全市场多 instrument | 强平瀑布、被迫订单流、延续/反转 | 官方无可信历史回填，只能前瞻积累；正式 Silver 当前只消费 BTC |
| 微观 Silver | orderbook、trade flow、OI-funding、volume profile、liquidation 各 `4,340` 行，2026-04-20 05:00 至 2026-08-29 11:15；BTC swap | 可组合的 15m 研究输入 | 跨跨度粗看密度不足，不能把首末时间当作连续性证明 |
| 历史 Gold | `3,600` 行：BTC 15m/1h，2026-07-22 至 2026-08-21 | 确定性历史 replay 输入 | 30 日、单标的、旧 bundle 资格受限 |
| 合约元数据 | 新代码支持不可变 instrument snapshot；`73` 个旧 `ELIGIBLE` bundle 均无 `instrument_contract_binding` | contract value、notional、fee、PnL 正确性 | 旧 bundle 可用于探索，不能冒充当前严格资本证据 |
| 宏观/链上/新闻 | 无现行正式链路 | regime、资金流与事件风险 | 当前为未实现，不得写成已有覆盖 |

历史 campaign `60e46f5e-e3e0-4090-b141-b53c92f1aa71` 的数据库状态为 `SUCCEEDED`，覆盖
2026-07-22 至 2026-08-21，manifest fingerprint 为
`81e42205278cc8ae56cd2c43ee60e480f82b114864a2d03cd79e35d9e8e59e88`。这只证明该旧 campaign
记录和产物存在，不会自动解决当前 instrument binding 或资本资格。

## 3. 当前已实现链与目标链必须分开

### 3.1 当前已实现事实

- Candle/funding staging 保存的是结构化行、source file/run lineage 和质量字段；它们**不普遍保存**
  原始 payload 或逐行 hash。
- Liquidation 与 trades 保存 raw payload；liquidation 另有 raw payload hash。官方历史 L2/trades 保存
  source row/partition lineage。
- BBO/books5 Bronze 保存结构化 top-of-book/top5 字段。`market_orderbook_payloads` sidecar 表存在，
  但本次未取得 runtime collector 已写入该 sidecar 的证据，不能声称 byte-exact payload 链已接通。
- Silver 已有五张 15m 微观表；现行 feature builder 正式消费 orderbook、trade flow、OI-funding，
  尚未完整消费 liquidation、volume profile 和历史 L2/trade 特征。
- Gold/research bundle、预注册、滚动评估和 sealed holdout 已有实现，但旧 bundle 与当前严格 instrument
  contract 资格不一致。
- LF-B1.2 的 A2a 来源仍是 `verification-only`；A2b formal reader、single-position reducer、golden
  ledger、publisher/checkpoint/recovery 尚未形成可运行的完整衍生品正式回测链。
- Paper/attribution 有代码接口，但本轮没有证明存在足量真实 fill 样本。

### 3.2 目标盈利链

```text
合法且有明确假设消费者的来源
  -> 可复核 Raw/Staging 与 available-time/lineage
  -> 正确单位、事件时间、去重和 point-in-time 的 Bronze/Silver
  -> 封存且可重复的 Gold/bundle
  -> 有经济机制的 Feature/Label
  -> 预注册 train/validation/OOS + 真实成本代理
  -> sealed test（仅通过者）
  -> 足量自然时间 paper/shadow + 真实 fill 成本校准
  -> 真人决定是否做极小资本 canary
```

当前最重要的断点在 Silver 之后：已有数据没有被充分转成可证伪机制和真实成本后证据。建设目标链
时不得把“应当具备”写成“现在已经具备”。

## 4. 运行时事实与当前阻断

采样时 PostgreSQL、Redis、NATS、Prometheus、Grafana、Loki、Promtail、Jaeger 等基础设施运行；
gateway、market、decision、execution、rdp-daemon、liquidations-daemon 和 microstructure-collector
七个应用容器均已退出约五小时。最新 trades/BBO/books5/liquidation 数据停在 11:31 左右。

最近 24 小时 continuity 记录中，microstructure 六个频道合计记录 12 次 `DISCONNECT`，
liquidation 记录 13 次 `DISCONNECT`，另有 7 次 `SHUTDOWN`，未见 `DROP` 事件。现有固定证据
没有按频道拆分，也没有证明断连后均成功恢复；`未见 DROP` 同样不能证明无缺口，仍须按
sequence/时间窗核对。

标准 derivatives 部署当前被一个未归属 NATS durable
`aats-codex_manual_resume-system_operator_command_responses` 安全阻断。它需要真人 owner/release review，
代码不得自动删除，自动化也不得用手工 Compose 绕过。因此“恢复采集”的当前状态是
`BLOCKED_BY_HUMAN_NATS_OWNERSHIP_DECISION`；工期只能从真人解除阻断后开始计算。

## 5. 立即停止或降级的工作

除非直接阻断不可回填采集、研究正确性、实验运行或资金安全，下列项目不占用主线：

- 为尚不存在的盈利候选建设完整 publisher/checkpoint、全站 UI 或通用权限平台；
- 没有具体收益实验消费者的广泛故障注入、框架抽象、指标版本和新表；
- 只增加状态卡、治理文案或测试数量，却不增加样本、特征、成本证据或淘汰假设；
- 在 10/10 已失败后继续调整同一组阈值，或再次窥视 sealed holdout；
- 以降低统计、成本或 live 门槛代替发现正 Alpha。

保留的最小工程边界只有：不可回填数据连续性、point-in-time/防泄漏、真实成本与可重复输入、
真实资金硬门。它们直接决定收益结论是否真实。

## 6. 盈利导向工作包

### P0：恢复不可回填前瞻数据

当前状态：`BLOCKED`。当前首个且唯一已知阻断是由真人决定未归属 durable 的 owner/删除/保留路径
并留下 release review 证据；流程尚未进入第二次 preflight、schema 和 app-up，正确处置并重跑后仍
可能暴露新 Gate。若无新增 Gate，解除后预计 1--2 个有效工作日完成：

- 通过标准模拟部署入口恢复获准的 OKX public collectors；
- 验证 BTC/ETH trades、BBO/books5、liquidation、OI/funding/mark 的精确 freshness 和缺口；
- 对不可历史回填的 liquidation/L2 标 `FORWARD_ONLY`；
- 不建设新的通用观测平台。

阻断期间不等待：继续使用现有历史数据推进 P1/P2。

### P1：释放已有微观数据价值（3--5 个有效工作日）

- 把 liquidation、volume profile、历史 L2/trade 的可用字段接入正式 feature registry；
- 输出 BTC/ETH 对称覆盖矩阵，明确 ETH 微观缺口而不伪造数据；
- 增加 15m/1h/4h 持有期、极端下行和执行后净收益标签；
- 完成 paper fill/订单生命周期的采样、对账和成本模型输入合同。

3--5 日只承诺实现和验证这些输入，不承诺获得足量 paper 样本；样本必须在 P4 以自然时间积累。

### P2：三个机制假设（5--10 个有效工作日完成首轮开发/验证）

1. **强平吸收 vs. 延续**：liquidation notional 与 trade flow/book imbalance 共振后，测试不同流动性
   regime 下的反转或延续；
2. **杠杆拥挤解除**：OI 变化、funding z-score、long/short 极值与 basis 联合，区分趋势扩张和去杠杆；
3. **流动性真空**：top5 depth、spread、volume profile 与主动成交失衡联合，测试突破续航和假突破。

每个假设在运行前冻结最低数据覆盖、缺失率、连续窗口、持有期、成本、参数数目和
`GO/ITERATE/KILL`。不满足数据门时不得因日历到期强行实验；达不到成本后阈值即淘汰，只有通过者
可以使用 sealed test。

### P3：最小宏观 regime 契约（2--4 周，与 P2 后半并行）

- Federal Reserve H.15 与 BLS v1 目前只被选为
  `SELECTED_FOR_OFFLINE_CONTRACT_SPIKE_PENDING_SOURCE_RECORD`，不是许可批准或持续采集授权；
- H.15 适配器必须版本化，保存 `fetched_at/available_at/revision`，记录历史 correction；官方已宣布
  2026 年 11 月起分阶段调整/退役现有 DDP 分发路径，不能把当前 CSV 路径写死；
- BLS v1 只提供获取时的 raw survey observations、没有完整 metadata。每次值标
  `CURRENT_AS_RETRIEVED`，同时记录 `revision_status=UNKNOWN`、`historical_vintage=UNKNOWN` 和
  `fetched_at`；只有存在官方发布时间证据时才填 `available_at`。必须结合官方 release
  calendar/archive，并从启用日起逐次快照，禁止把今天取得的值回填成过去可得值；
- 先验证利率期限结构、政策变化、CPI/就业发布窗口是否能过滤 crypto 信号；不能改善 OOS 时停止；
- Cboe VIX、链上、ETF flow、新闻等只有许可和边际价值审查通过后才进入实现。

### P4：前向 paper 与资本建议（至少 4--8 周自然时间）

- 候选须在未参与选择的数据、不同 regime、BTC/ETH 和滚动窗口中保持成本后正收益；
- 每个候选预先冻结最小自然时间、最小完整订单生命周期/成交数、置信下界或 DSR、最大回撤、尾部
  损失、换手、容量及 replay-paper 偏差门；
- paper 必须积累真实 fill、取消、拒单、延迟、fee/funding 和容量样本；工程测试不能替代自然样本；
- 若无候选通过，结论是淘汰或修订机制，不是放宽 live Gate；
- 只有全部门槛通过后才形成真人审批的极小资本 canary 建议。最早合理量级仍是 3--6 个月，且不
  保证盈利；实际时间取决于 P0 真人阻断和市场自然样本。

## 7. 数据源准入现状

现行来源决策见
[`AATS Research OS 来源准入与收益优先级纠偏`](../task/aats_research_os_source_access_and_profit_priority_correction_2026_08_29.md)。
Binance 已排除。Coinbase 公开行情因现行条款禁止 Market Data 用于 AI/ML 而拒绝；Kraken 的适用
地域与保存/派生/AI 权利尚不清楚，维持未选择。第二验证来源继续为 `UNBOUND`：不阻断 OKX 单源
Alpha 研发，但所有跨来源稳健性、lead-lag 和价格发现结论保持 `UNKNOWN`。

## 8. 阶段完成定义

RDP 不能以“代码写完”宣告盈利阶段完成。必须同时满足：

1. 不可回填数据持续积累，按 channel/instrument 的缺口可复核；
2. 至少三个机制假设完成预注册、成本后滚动 OOS，并留下 `GO/ITERATE/KILL` 证据；
3. 至少一个候选通过 sealed test，且未以该 holdout 继续选参数；
4. 该候选完成预注册的 paper 自然时间和订单/成交样本门，净收益置信下界或 DSR、最大回撤、尾部、
   换手、容量和 replay-paper 偏差同时达标；
5. 外部许可、真人审批和真实资金 Gate 如实保持，自动化不执行 live。

在第 4 项出现前，平台可以是合格研究系统，但不能称为接近稳定盈利。

## 附录 A：运行证据与复核方法

| 证据 ID | 来源 | 只读口径 | 本文使用 |
| --- | --- | --- | --- |
| DB-E1 | `aats_research` | 对表执行 `count(*) / min(ts) / max(ts) / distinct symbol`；表包括 staging/silver K 线、funding、bronze trades/BBO/books5/OI/mark/long-short、raw liquidations 和五张微观 Silver | 第 2 节行数、首末时间和 symbol scope |
| DB-E2 | `staging.official_l2_history`、`staging.official_trade_history` | `count(*) / min(ts) / max(ts) / pg_total_relation_size` | 历史 L2/trades 规模；relation size 包含数据库关系开销，不是下载文件大小 |
| DB-E3 | `meta.historical_campaign_runs` | 按 campaign UUID 读取 status、coverage、requested_days 和 `manifest_fingerprint` | 30 日历史 campaign provenance |
| DB-E4 | `meta.dataset_bundles` | 按 status 计数，并检查 `eligibility_report` 是否含 `instrument_contract_binding` | 73 个旧 ELIGIBLE bundle 的绑定缺口 |
| RT-E1 | WSL2 `docker ps -a` | 只读查看容器状态，不启动/停止 | 七个应用停止、基础设施运行 |
| RT-E2 | `meta.collector_continuity_events` | 采样前 24 小时按 collector/channel/event_type 计数 | disconnect/reconnect/shutdown 事实 |

上述 SQL、结构化结果、UTC 采样时间、数据库时区和容器状态保存在
[`rdp_profitability_data_snapshot_2026_08_29.json`](evidence/rdp_profitability_data_snapshot_2026_08_29.json)。
SHA-256 为 `f37fff372560a0de8b510076832a623051ae492f857486e340ae7a34357badde`，防止未来数据库
变化后把新结果冒充本次快照。

文中 10/10 候选结论的研究 artifact 与成本代理口径仍以
[`真实收益差距评估`](profitability_gap_assessment_2026_08_25.md)及对应 immutable experiment artifact 为准；
本轮没有重新运行或解封 holdout。
