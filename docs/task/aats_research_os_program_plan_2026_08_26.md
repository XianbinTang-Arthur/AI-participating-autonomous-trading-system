# AATS Research OS 独立建设计划书

> 文档状态：立项规划基线；项目启动已条件性批准，G0 Gate 仍为 `OPEN / NOT PASSED`
> 版本：1.0
> 最后核对：2026-08-26（代码基线 `40dc6817861a1ddfd92cc8a01d2b9ce87af523aa`，分支 `main`）
> 核对范围：当前静态代码、ORM、工作流、现行 RDP/数据恢复/收益就绪文档，以及文末列出的成熟平台官方资料
> 运行边界：本计划未重新验证当前容器、数据库覆盖、采集新鲜度、账户、仓位、订单、Kill Switch、active parameter 或交易所状态
> 授权边界：本计划不授权创建真实资金写路径、不授权 live profile、不授权下单、不授权修改 Runtime 活动参数，也不把“目标高盈利”表述为收益保证

> 现行 G0 决策：[`G0 立项决议记录`](aats_research_os_g0_decision_record_2026_08_26.md)
> 下一步执行：[`G0 收口与 G1 启动计划`](aats_research_os_g0_closure_and_g1_kickoff_plan_2026_08_26.md)

## 0. 决策摘要

正式立项一个与现有 RDP 隔离的 `AATS Research OS`。它不是 RDP V4，也不是在现有 101 张
RDP ORM 表和 10 个 workflow 上继续扩建，而是一个独立的“研究证据生产与资本决策支持系统”。

建议采用以下组织与安全边界：

1. 新平台使用独立代码库、独立数据库、独立对象存储命名空间、独立部署和独立服务身份；
2. 现有 RDP 冻结为受维护的 Legacy System，只承担历史证据、业务规则对照、双跑比较和迁移回退参照；
3. Research OS 不持有交易所下单密钥，不直接写 AATS Runtime 数据库、运行参数或交易接口；
4. Research OS 只输出不可变、可验证、可签名的 `Candidate Release Package`；
5. AATS Runtime 只能由 Operator 在独立审批后主动拉取、校验、prepare、commit、ACK/readback；
6. 没有通过完整证据门的候选时，`NO RELEASE` 是正确结果；
7. 本文件获批只代表 G0 立项准备获批，后续 G1--G8 仍须逐门取得授权。

必须区分两种成功：

- **平台成功**：系统能够真实记录市场、阻止时间泄漏、完整保存失败试验、重现结论、校准执行并拒绝伪收益；
- **资本成功**：至少一个候选经过数据、统计、执行、前向、组合、风险和治理门后，仍具有成本后、容量调整后的正期望。

第一种成功可以通过工程交付实现；第二种取决于市场中是否存在可获取、可持续、可执行的优势，
不能由平台建设承诺。

## 1. 为什么必须独立重建

### 1.1 当前事实

当前 `HEAD` 的静态复核结果是：

- `aats/data_platform/` 有 228 个 Python 文件；
- `RdpBase.metadata` 当前声明 101 张 ORM 表；
- `configs/rdp_workflows/` 有 10 个 workflow 定义；
- 现有系统已经覆盖采集、staging/bronze/silver/gold、恢复、Research Factory、回放、归因、
  execution realism、候选、推荐、审批、发布、观察和 UI 工作台；
- RDP V3 的重点是统一读模型、队列透明度和运营控制面，没有重写数据、特征、统计和执行内核；
- 历史收益审查在当时基线确认 10/10 个唯一候选失败，不能当作当前运行状态，但足以证明
  “工程可运行”不等于“已经存在赚钱策略”；
- 2026-08-26 的历史恢复现场快照曾报告一日 trade+L2 约增加 8.10 GB PostgreSQL 数据，
  raw 约 0.592 GB；该结果是历史容量快照，不是当前磁盘事实，但足以否决“把大量原始事件继续
  直接扩进 PostgreSQL”这一长期架构。

相关当前入口与历史证据：

- [RDP 当前模块说明](../../aats/data_platform/README.md)
- [RDP 代码模块参考](../rdp/module_reference.md)
- [RDP V3 架构设计](../design/rdp_platform_v3_architecture_2026_08_25.md)
- [历史数据恢复运行手册](../operations/rdp_historical_data_recovery_runbook.md)
- [收益差距评估](../code_review/profitability_gap_assessment_2026_08_25.md)

旧文档中的 98 张表、225 个 Python 文件是其各自基线事实；本计划使用当前 `HEAD` 重新派生的
101/228，不回写历史快照。

### 1.2 结构性问题

现有系统的问题不是功能数量不足，而是大量功能仍围绕表、脚本和工作流扩展：

1. 原始事实、标准化事件、研究数据集、特征、实验、仿真和运行反馈之间缺少一个统一的领域合同；
2. `event_time`、交易所发布时间、本地接收时间、研究可见时间没有成为所有研究的强制时序约束；
3. symbol、数量、合约面值、名义价值、OI、手续费、资金费率和保证金语义尚未统一绑定到
   版本化 Instrument Master；
4. 大体量事件进入关系数据库后，存储、索引、WAL、备份和回填成本按窗口近似线性增长；
5. 采集了数据不等于 Research Factory 能以正确单位和时间消费；
6. bar proxy、trade replay、L2 replay 和 paper fill 的证据等级尚未形成统一等级体系；
7. 研究、纸面和 Runtime 仍可能拥有不同的特征、订单、费用和状态语义；
8. 当前控制面以“运行流程”为中心，世界级平台必须改为以“完整证据包”为中心。

### 1.3 不采用 Big Bang 替换

独立重建不等于一次性切断旧系统。Research OS 必须并行建设、只读接入、逐能力双跑；在新能力
通过证据门之前，旧 RDP 不删除、不改写历史证据，也不承担新平台尚未具备的隐式职责。

### 1.4 应提炼而不是重写的资产

“从零建设”指从新的领域边界和真源出发，不代表丢弃已经验证过的正确性资产。下列能力应通过
Anti-Corruption Layer、黄金样例或版本化公共包提炼，而不是原样复制旧表和工作流：

| 现有资产 | 可提炼价值 | 新平台处理方式 |
| --- | --- | --- |
| [`data_governance/contracts.py`](../../aats/data_platform/data_governance/contracts.py) | `SourceKind`、`TruthTier`、source/bundle fingerprint、gap manifest | 升级为版本化 SourceAsset/Dataset Contract |
| [`data_governance/eligibility.py`](../../aats/data_platform/data_governance/eligibility.py) | source-aware 资格、因果时间、未分类缺口失败关闭 | 升级为可审计 Policy 对象，不保留硬编码矩阵作为最终真源 |
| [`data_governance/continuity.py`](../../aats/data_platform/data_governance/continuity.py) | generation、connect/disconnect/drop/flush、receive/sample time | 立项第一天旁路采集，避免不可回填事实继续丢失 |
| [`data_governance/archive.py`](../../aats/data_platform/data_governance/archive.py) | Parquet 不可覆盖、manifest、SHA-256、恢复核验 | 改为对象存储和内容寻址，保留不变量与恢复测试 |
| [`data_governance/historical_campaign.py`](../../aats/data_platform/data_governance/historical_campaign.py) 与 [`historical_gold.py`](../../aats/data_platform/data_governance/historical_gold.py) | 容量门、checkpoint、source-aware Gold 和确定性 fingerprint | 作为迁移/回归输入；不把旧 Gold 自动升级为新资格 |
| [`research_factory/validation`](../../aats/data_platform/research_factory/validation) | holdout claim、purged walk-forward、bootstrap、Holm、DSR | 提炼为独立统计裁决内核，保留全部失败试验 |
| [`execution_realism`](../../aats/data_platform/execution_realism) | bar/L2/paper 校准基线和边界样例 | 作为 Execution Twin 的回归向量，不宣称为最终撮合真值 |
| [`governance/rdp_runs_db.py`](../../aats/data_platform/governance/rdp_runs_db.py) | Run/Attempt/Step/Event、单调终态、取消、重试 | 保留生命周期语义；不保留单执行槽作为目标计算架构 |
| [`schemas/exchange.py`](../../aats/schemas/exchange.py) 与 [`quantity_rules.py`](../../aats/services/execution_engine/quantity_rules.py) | Runtime 已有的 instrument、contract value 和数量转换 | 提炼为版本化公共金融合同，避免研究侧另造单位体系 |

当前 source-aware historical campaign/Gold 已存在静态实现；G0 需要验证的是新旧合同如何迁移，
而不是把它继续列为“完全缺失”。同样，现有统计工具存在不代表已有合格候选，现有参数状态机存在
也不代表 Runtime ACK/readback 已接通。

### 1.5 G0 必须重新取得的现场基线

本次没有读取运行数据库、容器或交易所。G0 启动审计必须重新取得以下事实，并保存时间、commit、
profile、脱敏环境指纹和证据位置：

- 当前目标数据库是否已应用 Batch B stage 19，以及实际 schema、体积和磁盘余量；
- 当前 source-aware Gold/campaign、archive、bundle、gap 和 coverage 状态；
- collectors 是否运行，trade/BBO/books5/OI/funding/mark/liquidation 的 freshness 与 continuity；
- 当前 Run/queue/失败原因、candidate、recommendation 和 sealed holdout；
- 当前 Runtime parameter generation、ACK/readback、Kill Switch、reconciliation；
- 当前模拟订单、成交、成本和净收益证据；
- OKX 及候选第二来源当前协议、下载能力、数据保存许可和限频规则。

任何未重新读取的项目保持 `UNKNOWN`，不能作为 G0/G1 已通过的前提。

## 2. 使命、目标函数与成功定义

### 2.1 使命

Research OS 的使命是持续提高发现真实收益、证伪虚假收益和保留执行后收益的能力，并把通过验证的
候选以最小权限、可回滚、可审计的方式交付给 AATS Runtime。

目标函数定义为：

```text
最大化：长期可实现净收益

可实现净收益
= 毛收益
- 手续费与返佣差异
- spread crossing
- 滑点与 adverse selection
- 市场冲击与容量折损
- 资金费率与借贷成本
- 未成交、部分成交和撤单成本
- 数据、算力和运行成本

约束：最大回撤、Expected Shortfall、流动性、容量、杠杆、保证金、
      集中度、可靠性、模型不确定性、权限与治理
```

不能用毛收益、回测 Sharpe、候选数量或任务通过数替代该目标。

### 2.2 第一版必须实现

- BTC/ETH 永续合约的可信市场事实层；
- OKX 主来源和至少一个独立参考来源的交叉核验能力；
- 版本化 Instrument/Venue Registry；
- 不可变原始归档、校验、血缘、修订和恢复；
- Point-in-time 数据集、Feature/Label Graph 和封存 holdout；
- 确定性事件回放、订单状态机、费用、资金费率、保证金、强平和容量模型；
- 预注册假设、完整 Trial Ledger、统计裁决和 Evidence Bundle；
- paper/shadow 逐订单校准和前向观察；
- 不可变、签名的 Candidate Release Package；
- 独立审批、ACK/readback、回滚、故障矩阵和审计；
- 以证据为中心的研究工作台。

### 2.3 第一版明确不做

- 不覆盖全部交易所、资产和另类数据；
- 不把微秒级 HFT 或共址做市作为首期竞争方向；
- 不把现有 101 张表逐表复制到新系统；
- 不先建设 Kubernetes、Service Mesh、多区域主动双活；
- 不同时引入 MLflow、Feast、Airflow、Optuna、Ray 等完整生态；
- 不开展没有经济机制和试验预算的大规模因子穷举；
- 不把 LLM 输出视为 alpha 或统计证据；
- 不让 AI 打开 holdout、批准资本、签名发布、解除 Kill Switch 或恢复 live；
- 不先建设大而全 UI；
- 不以“运行完整平台”单一大工作流组织所有任务。

## 3. 首个盈利楔子

世界级不等于第一天覆盖最多市场，而是先在狭窄范围做到世界级正确，再扩大范围。

| 方向 | 机会 | 主要制约 | 决策 |
| --- | --- | --- | --- |
| 微秒级做市/HFT | 高频、可高周转 | 无共址、公共网络、队列真值不足、竞争极强 | 首期拒绝 |
| 单交易所 5 分钟--4 小时永续 | 数据与当前经验匹配，可研究资金费率、OI、订单流、流动性状态 | 需真实成本和前向校准 | 首个主楔子 |
| 跨交易所 basis/funding/lead-lag | 机制相对清晰，可提供独立事实源 | 多交易所语义、时钟、成本和账户复杂度 | G1 后并行验证 |
| 期权、链上、新闻、宏观 | 可增加不同周期信息 | 许可、时间戳、可交易性和研究自由度显著增加 | 只在明确假设后引入 |

首期建议范围：

- 主产品：`BTC-USDT-SWAP`、`ETH-USDT-SWAP`；
- 主来源：OKX public/history 与 AATS 自身模拟/影子执行反馈；
- 独立来源：Binance USDⓈ-M Futures 的 `BTCUSDT`、`ETHUSDT`，仅作为有条件批准的公共只读
  验证源；许可和地域书面结论通过后才可持续采集；Bybit 仅作为需书面许可的备选；
- 研究周期：5 分钟至 4 小时；
- 首批机制族：跨来源领先滞后、funding/basis/OI 结构错配、订单流与流动性状态、
  regime-aware 风险过滤、execution alpha；
- 每个机制族必须先提交 Hypothesis Card、数据需求、成本模型、失败条件和 Trial Budget。

## 4. 目标体系结构

```text
外部 public/history/规则事实                 AATS Runtime 只读反馈
          |                                         |
          v                                         v
  Capture Adapter + 本地持久 spool        append-only telemetry export
          |                                         |
          +-------------------+---------------------+
                              v
┌──────────────────────────────────────────────────────────────────┐
│ Market Truth Fabric                                              │
│ Immutable Raw Ledger -> Canonical Events -> Quality/Gap/Revision │
│ -> Dataset Snapshots -> Point-in-time Feature & Label Graph      │
└──────────────────────────────┬───────────────────────────────────┘
                               │ immutable snapshot IDs
┌──────────────────────────────▼───────────────────────────────────┐
│ Scientific Research OS                                          │
│ Hypothesis -> Protocol -> Campaign -> Trial Ledger -> Evidence   │
└──────────────────────────────┬───────────────────────────────────┘
                               │ candidate evidence
┌──────────────────────────────▼───────────────────────────────────┐
│ Execution Digital Twin                                          │
│ Event Replay -> Order/Margin/Fee -> Fill/Latency/Impact -> Calib │
└──────────────────────────────┬───────────────────────────────────┘
                               │ executable evidence
┌──────────────────────────────▼───────────────────────────────────┐
│ Portfolio, Model Risk & Capital Governance                       │
│ Capacity -> Tail Risk -> Failure Envelope -> Signed Release      │
└──────────────────────────────┬───────────────────────────────────┘
                               │ quarantine registry
                               v
                     Operator-authorized pull/verify
                               |
                               v
          AATS Runtime prepare -> ACK -> commit -> readback
```

横向控制面负责身份、资源配额、任务、血缘、审计、可观测性和灾难恢复，但不能改写研究结论、
自动批准候选或绕过资本门。

### 4.1 有界上下文

| 有界上下文 | 核心职责 | 明确不负责 |
| --- | --- | --- |
| Instrument & Venue Registry | 合约规格、交易规则、币种、tick/lot、乘数、有效期 | 行情采集、策略逻辑 |
| Observation Capture | 原始数据、接收时间、连接代次、序列和时钟质量 | 修正数据、生成特征 |
| Immutable Market Ledger | 原始对象、校验、分区、修订事件 | 研究查询优化 |
| Canonical Market Model | 供应商格式到统一事件和单位 | 静默丢弃或修正原始字段 |
| Data Quality & Lineage | 缺口、重复、乱序、异常、隔离、修订、血缘 | 用默认值伪造完整性 |
| Feature & Label Graph | PIT 特征、标签、依赖和版本 | 访问未来数据 |
| Research Protocol | 假设、预算、失败条件和窗口 | 直接选择上线参数 |
| Experiment & Evidence | Campaign、Trial、统计检验、证据包 | 删除或覆盖失败结果 |
| Execution Digital Twin | 回放、订单、费用、保证金、容量、校准 | 连接交易所下单接口 |
| Portfolio Research | 多策略依赖、容量、风险贡献和压力测试 | 真实资金调仓 |
| Model Risk & Release | 独立审核、签名、期限、回滚、发布状态 | 写 Runtime 内存或数据库 |
| Runtime Feedback | 接收脱敏订单、成交、偏差和失效反馈 | 获取 Runtime 交易权限 |

有界上下文之间通过版本化合同和不可变产物交换，不共享内部数据库表。

## 5. 核心领域合同

### 5.1 Canonical Event Envelope

每个市场事件至少包含：

```text
event_id
source_id
venue
instrument_version_id
event_type
event_time
exchange_publish_time
receive_time
available_time
source_sequence?
capture_generation
capture_ordinal
raw_object_id
normalizer_version
quality_state
correction_of?
```

必须区分：

- `event_time`：市场事件发生时间；
- `exchange_publish_time`：交易所公布时间；
- `receive_time`：本地收到时间；
- `available_time`：策略或研究实际能够使用该事实的最早时间。

任何研究连接都必须满足：

```text
feature.available_time <= decision_time
```

平台同时保留 valid time 和 system time，并明确生成：

- `as_observed`：重现历史当时可见事实，用于无泄漏回测；
- `best_known`：使用后来修订后的最佳历史事实，用于审计与诊断。

二者不得混用。

### 5.2 InstrumentVersion

必须版本化保存：

- base、quote、settle currency；
- spot、linear、inverse、option 等类型；
- contract value、multiplier；
- tick、lot、最小下单量；
- 数量、名义价值、PnL、保证金和强平换算公式；
- fee tier 与交易规则引用；
- 生效、失效时间、来源和校验状态。

任何成交额、强平额、OI、资金占用、费用和收益计算都必须解析到事件发生时有效的合约版本。

### 5.3 Dataset Contract 与 Dataset Snapshot

每类数据产品必须声明：

```text
dataset_spec_id / schema_version / owner / source_authority
event_and_available_time_semantics / unit_contract
partitioning / expected_cadence / max_lateness
completeness_policy / gap_policy / correction_policy
retention / legal_usage / quality_SLO
allowed_consumers / forbidden_claims
```

Snapshot 必须绑定 raw manifest、Instrument Master、normalizer、quality policy、半开时间窗口和
内容 fingerprint。大型数据通过对象清单和 Arrow/Parquet 交换，不通过 API 搬运。

### 5.4 FeatureDefinition

每个特征必须声明：

- 经济含义、单位、方向和预期持有周期；
- 输入 Dataset Contract；
- window、lag、availability、warm-up 和缺失策略；
- transform 版本和依赖图；
- offline/shadow/live 计算实现；
- 数值容差和漂移规则；
- 允许的研究与 Runtime 消费者。

### 5.5 Hypothesis、Protocol 与 Evidence Bundle

```text
Hypothesis
  -> ResearchProtocol
      -> ExperimentCampaign
          -> Trial / Evaluation / HoldoutClaim
              -> EvidenceBundle
                  -> Candidate
```

ResearchProtocol 必须在运行前固定经济机制、数据、搜索空间、最大试验次数、成本、主要指标、
否决指标、train/validation/holdout、统计方法和提前停止条件。

Evidence Bundle 至少绑定：

```text
hypothesis_id / protocol_id / complete_trial_ledger
code_commit / source_tree_digest / OCI_digest / dependency_lock_digest
dataset_snapshot_ids / instrument_snapshot_id / feature_graph_digest
label_digest / simulation_model_digest / canonical_config / seeds
quality_report / statistical_evaluations / execution_calibration
capacity_curve / risk_and_failure_envelope / artifact_hashes
creator / reviewer / timestamps
```

所有失败、崩溃、无数据和被剪枝试验也必须进入 Trial Ledger。

### 5.6 Candidate Release Package

```text
release_id / candidate_id / evidence_bundle_id
strategy_artifact_digest / required_runtime_contract_version
feature_contract_digest / parameters / allowed_instruments
risk_envelope / capital_ceiling / leverage_ceiling / order_rate_ceiling
applicable_regimes / drift_thresholds / stop_conditions / expiry
rollback_release_id / approvals / signature
```

风险边界是 Runtime 可以进一步收紧、不能放宽的上限。

## 6. 与 AATS Runtime 的隔离

### 6.1 物理与权限隔离

- 独立数据库和对象存储命名空间；
- 独立部署、CI/CD、服务身份和密钥；
- Research OS 不持有下单、提现、划转或 Runtime 管理凭证；
- 研究 worker 默认禁止访问 Runtime 数据库和管理 API；
- 可复现实验默认关闭外网；
- Runtime 不导入 Research OS 应用服务代码；
- 共享纯领域模型时，只消费经过审查、版本锁定和签名的包。

### 6.2 单向集成

```text
Runtime
  -- append-only、脱敏 telemetry --> Research OS

Research OS
  -- signed immutable release --> Quarantine Registry

Runtime Control Plane
  -- operator-authorized pull/verify/stage --> Runtime Workers
```

Research OS 不主动调用 Runtime 参数写接口。Runtime 必须独立验证签名、hash、环境、schema、
风险上限、审批、过期时间和 rollback 目标，再执行：

```text
prepare -> expected workers ACK -> commit -> readback -> observation
```

错代次、错 hash、部分 ACK、超时、readback 不一致或状态未知一律失败关闭。

## 7. 执行数字孪生

数字孪生必须包含版本化的纯领域内核：

- 确定性时间推进和事件排序；
- L1/L2/trade/bar 分层回放；
- 限价、市价、IOC、post-only、撤单和修改；
- 完整订单生命周期和竞态；
- 部分成交、queue-ahead、spread crossing、adverse selection；
- 手续费、返佣、资金费率；
- 线性/反向合约 PnL；
- 保证金、风险率和强平；
- 网络、处理、确认和撤单延迟；
- 市场冲击、容量、拒单、限频与交易所中断；
- 与 paper/shadow 订单逐笔绑定的校准器。

证据等级必须明确：

| 层级 | 可以支持 | 不得声称 |
| --- | --- | --- |
| Bar Proxy | 早期方向性筛选 | 成交概率、盘口容量、真实滑点 |
| Trade Replay | 主动成交时机和订单流研究 | 被动订单真实队列 |
| L2 Replay | 深度内部分成交、队列近似、局部容量 | 完整撮合和自身冲击真值 |
| Paper/Shadow Calibration | 校准延迟、成交率、费用和偏差 | 自动证明未来盈利 |

Python 用于研究表达、数据处理和实验编排；性能基准证明需要后，再将确定性事件排序、订单状态机、
盘口回放等关键路径迁移到 Rust。不能为了技术偏好提前引入跨语言复杂度。

## 8. 物理技术路线

逻辑合同从 G1 起稳定；物理实现按负载分阶段演进。

### 8.1 第一阶段：正确性内核

- 对象存储：S3/MinIO；
- 文件与内存格式：Parquet + Arrow；
- 控制面与元数据：PostgreSQL；
- 本地研究：DuckDB + Polars；
- 研究 SDK：Python；
- 仿真：先 Python 正确性原型，性能热点再迁 Rust；
- 环境：依赖锁定的 OCI 镜像；
- API：REST；内部稳定合同使用 Protobuf；
- 编排：PostgreSQL lease、幂等状态机、heartbeat、retry budget、资源配额；
- 可观测：OpenTelemetry、Prometheus、结构化日志；
- 产物签名：独立签名身份和可验证供应链记录。

### 8.2 规模触发后才引入

| 组件 | 引入条件 |
| --- | --- |
| Iceberg | 需要大规模 snapshot time-travel、schema evolution、分区治理和 WAP 时 |
| ClickHouse | DuckDB/对象扫描经基准证明无法满足交互与批量 SLA 时 |
| Redpanda/Kafka | 多采集器、持续高吞吐和多消费者解耦成为真实瓶颈时 |
| Dagster | 数据资产依赖、回填和 materialization 已超过简单状态机能力时 |
| Ray | 独立实验已经可安全并行，且单机资源成为主瓶颈时 |
| Feast | 多个在线特征消费者出现，共享库已无法保证 offline/online 一致时 |
| Kubernetes | 服务规模、团队边界和可用性要求证明 Compose/单节点不再适用时 |

成熟实践只用于校验原则：QuantConnect 将 Idea、Research、Backtest、Paper、Live 明确分段；
NautilusTrader 强调回测与 live 共用核心组件和策略语义；Feast 展示 point-in-time join；Iceberg
提供 snapshot/time-travel；OpenLineage 使用 Dataset、Job、Run 表达血缘。首期不因此直接部署
这些完整产品。

## 9. 四条项目工作流与 WBS

| 工作流 | 目标 | 工作包 |
| --- | --- | --- |
| A. Market Truth Fabric | 证明市场当时发生了什么 | W1--W3 |
| B. Research & Execution Science | 证明结论有效且可成交 | W4--W8 |
| C. Capital Governance & Reliability | 证明成果可安全交付资本 | W9--W10 |
| D. Research Product Experience | 让研究者理解、复现和审阅证据 | W11 |

### W0：计划治理

- W0.1 项目章程、成功定义和非目标；
- W0.2 预算、Owner、决策权和否决权；
- W0.3 Legacy 冻结策略；
- W0.4 数据许可、隐私、法律与交易所条款；
- W0.5 ADR、风险登记、证据目录和变更分级；
- W0.6 新旧系统隔离及威胁模型。

### W1：市场领域模型

- W1.1 Instrument/Venue Registry；
- W1.2 线性/反向合约数量、面值、名义价值、PnL、保证金和强平公式；
- W1.3 四类时间与双时间模型；
- W1.4 唯一性、sequence、修订和去重规则；
- W1.5 交易所状态与规则版本；
- W1.6 黄金合约、事件和金融恒等式测试。

### W2：采集与不可变归档

- W2.1 WebSocket、REST、历史文件适配器；
- W2.2 本地持久 spool、checkpoint、重连、连接代次和补洞；
- W2.3 append-only raw 对象；
- W2.4 Parquet 分区、manifest、checksum 和 schema version；
- W2.5 hot/warm/cold 分层；
- W2.6 retention、备份、恢复和成本计量；
- W2.7 第二来源交叉验证。

### W3：数据治理

- W3.1 Dataset Contract；
- W3.2 freshness、sequence、late/correction SLO；
- W3.3 quarantine、gap 和有效零；
- W3.4 来源差异、单位与价格异常；
- W3.5 raw 到 experiment 全血缘；
- W3.6 研究窗口资格证明。

### W4：PIT 数据集与特征图

- W4.1 Feature/Label Registry；
- W4.2 `available_time` as-of join；
- W4.3 依赖 DAG；
- W4.4 offline/shadow 共用代码；
- W4.5 单位、窗口、缺失、延迟和消费者声明；
- W4.6 Dataset Snapshot 与确定性 fingerprint；
- W4.7 future-leak 自动攻击测试。

### W5：执行数字孪生

- W5.1 确定性事件调度；
- W5.2 订单与账户状态机；
- W5.3 L2、partial/no-fill 和队列近似；
- W5.4 fee、funding、margin、liquidation；
- W5.5 延迟与竞态；
- W5.6 容量、冲击和 adverse selection；
- W5.7 prediction 与 paper lifecycle 绑定；
- W5.8 按订单类型、规模和 regime 校准。

### W6：Scientific Research OS

- W6.1 Hypothesis Card；
- W6.2 Research Protocol、Campaign、Trial Budget；
- W6.3 train/valid/sealed holdout；
- W6.4 purge、embargo、walk-forward；
- W6.5 bootstrap、DSR、多重检验和 PBO；
- W6.6 全试验记录；
- W6.7 Evidence Bundle；
- W6.8 独立复现和复核。

### W7：组合与资本研究

- W7.1 候选相关性和共同失效；
- W7.2 风险预算、容量和集中度；
- W7.3 regime 稳定性；
- W7.4 尾部情景和压力测试；
- W7.5 Champion/Challenger；
- W7.6 降级、暂停和退出规则。

### W8：Paper、Shadow 与反馈

- W8.1 共用策略/领域内核的 paper adapter；
- W8.2 影子信号与 Runtime 状态对齐；
- W8.3 prediction 到 order/fill/PnL 精确归因；
- W8.4 模型和执行漂移；
- W8.5 反馈反向校准；
- W8.6 冻结候选的前向观察。

### W9：发布与资本治理

- W9.1 Candidate Release Package；
- W9.2 内容签名、审批和过期；
- W9.3 prepare/commit/ACK/readback；
- W9.4 版本回滚；
- W9.5 Kill Switch 和短期交易许可；
- W9.6 故障矩阵；
- W9.7 资本分层和人工恢复。

### W10：可靠性与安全

- W10.1 RBAC、服务身份和最小权限；
- W10.2 Secret 隔离；
- W10.3 append-only/WORM 审计；
- W10.4 依赖锁、SBOM、镜像和产物签名；
- W10.5 SLO、告警和成本观测；
- W10.6 RPO/RTO、备份与恢复演练；
- W10.7 故障注入、安全测试和独立审查。

### W11：研究产品界面

- W11.1 Data Truth Room；
- W11.2 Hypothesis Lab；
- W11.3 Experiment Studio；
- W11.4 Execution Twin；
- W11.5 Portfolio & Capital；
- W11.6 Release Governance；
- W11.7 队列、成本、进度、取消和证据详情；
- W11.8 UTF-8 中文、无障碍和危险动作保护。

### W12：迁移与退役

- W12.1 Legacy 资产清单；
- W12.2 保留证据、转换迁移、只读包装、淘汰分类；
- W12.3 ID、时间、单位和 lineage crosswalk；
- W12.4 同数据双跑与差异解释；
- W12.5 能力级切换；
- W12.6 Legacy 只读封存；
- W12.7 最终退役审计。

## 10. 阶段、投入与硬退出门

投入用 FTE-month 表示，用于资源估算，不代表到期自动完成。阶段只由证据过门。

### G0：立项授权

计划投入：2--3 个日历周，约 1--2 FTE-month。

交付：项目章程、范围、RACI、预算档位、数据许可、威胁模型、Legacy 冻结规则、ADR-001--ADR-010。

退出门：

- Program Owner、Data Owner、Quant Owner、Execution Owner 和独立 Risk 否决人明确；
- 新平台不存在 live 写路径或下单凭证；
- 数据具备合法采集、保存和研究权；
- 新旧平台物理和权限边界可实施；
- 团队接受“无正候选即不进入资本阶段”；
- 预算至少覆盖 G1--G4，而不是只够搭 UI 或采集数据。

G0 未通过则不创建大规模基础设施。

### G1：Canonical Truth Kernel

计划投入：4--7 FTE-month。

交付：Instrument Master、Canonical Event、raw object store、核心采集器、Dataset Contract、
质量、血缘和恢复工具。

退出门：

- 核心事件四类时间和 instrument version 覆盖率 100%；
- 数量、名义价值、fee、funding、PnL、margin 黄金样例全部通过；
- 原始分区 checksum、manifest、来源和时间边界覆盖率 100%；
- 资本资格窗口未分类缺口为 0；
- 同一原始快照在隔离环境重建相同标准化 fingerprint；
- 断流、drop、时钟异常和未知状态显式失败；
- 隔离恢复不修改源对象；
- 经过实测容量和成本基准。

### G2：Point-in-time Research Substrate

计划投入：3--5 FTE-month。

交付：Feature/Label Registry、PIT join、Dataset Snapshot、分段和 holdout 权限。

退出门：

- future-leak 攻击测试逃逸数为 0；
- 相同 input/code/config 产生相同 fingerprint；
- raw 到 feature 的 lineage 完整率 100%；
- 特征单位、窗口、缺失、可用时间和消费者 100% 机器可读；
- offline/shadow 同输入在预注册容差内一致；
- 普通研究身份无法读取 sealed holdout。

### G3：Execution Digital Twin

计划投入：5--8 FTE-month。

交付：确定性回放、订单/账户模型、fee/funding/margin/liquidation、L2 fill、paper telemetry 和校准器。

退出门：

- 现金、仓位、数量、费用和保证金恒等式全部通过；
- event ordering、same-bar、partial/no-fill、撤单竞态、拒单、限频和重启黄金场景通过；
- 当前门限只作为最低起点：匹配 paper order 至少 20、生命周期有效率 100%、
  fill ratio MAE 不高于 0.20、均价误差不高于 10 bps、费用误差不高于 1 bps、
  command-to-terminal p95 不高于 5 秒；
- 误差按订单类型、规模和 regime 分层；样本增长后必须重新收紧；
- 无法校准的候选不得进入 G5。

### G4：Scientific Research OS

计划投入：4--7 FTE-month。

交付：预注册、Campaign、Trial Ledger、统计裁决、Evidence Bundle 和独立重放。

退出门：

- 全部成功、失败、崩溃、无数据和剪枝试验记录率 100%；
- 未登记 holdout 访问为 0；
- 一次性 holdout 攻击测试通过；
- 独立 worker 能从 Evidence Bundle 复现；
- 每个候选具有经济机制、失败条件、成本、容量和适用状态；
- 所有候选被淘汰时，系统仍能正确完成科学流程。

### G5：Alpha Discovery

计划投入：每个机制族 2--4 FTE-month，另受数据日历约束。

候选门在试验前登记，至少要求：

- 所有成本后 edge 为正；
- 置信区间下界或等价稳健门为正；
- purged walk-forward、bootstrap、DSR 和多重检验通过；
- 回撤不超过候选风险预算；
- 容量高于拟配置资本并留安全余量；
- 所需数据在 Runtime 可按同一时间语义获得；
- holdout 未用于调参。

试验预算耗尽后仍无候选，暂停新增基础设施和扩展搜索，召开战略复盘；不得降低阈值或反复修改
同一机制后重新命名。

### G6：Frozen Forward Paper

前置：至少一个 G5 合格候选。计划投入 2--4 FTE-month；观察日历不可用人员压缩。

退出门：

- 参数、代码、特征、数据、风险和成本模型冻结；
- 前向期间没有回看后调参；
- 实际执行偏差在 G3 校准包络内；
- 成本后净收益、回撤、漂移、fillable 和 partial fill 均通过预注册门；
- 订单 100% 可归因到候选、特征、数据和发布版本；
- 任一 abort、断链或越界即淘汰/退回，不通过延长窗口等待翻盘。

### G7：Capital Delivery Readiness

计划投入：3--5 FTE-month。

退出门：

- 所有 Runtime 角色 ACK/readback 精确一致；
- stale、partial ACK、TTL 过期和错 hash 只能失败或回滚；
- Redis、NATS、execution restart、stale generation、permission expiry 等隔离故障矩阵通过；
- reconciliation、Kill Switch、人工恢复、备份、RPO/RTO、安全测试通过；
- readiness 不含 `UNKNOWN`；
- 未参与候选开发的 Reviewer 完成独立复现；
- Risk、Security、Operator 明确签字。

### G8：受控资本 Canary

G8 不因本计划获批而自动授权，必须另行审计和人工批准。

原则：单交易所、单品种、逐仓、低杠杆、无提现/划转权限、极小资本、双签、人工恢复、逐级扩容。
现有历史 canary 数字不得自动继承，必须根据当时 Runtime、市场、费用和风险证据重新批准。

立即停止条件包括：

- 任一 readiness 为 `UNKNOWN`；
- 参数、代码、特征或数据版本不一致；
- freshness、continuity、reconciliation、Kill Switch 或权限状态异常；
- 不可归因订单/仓位；
- 达到损失、回撤、敞口或执行偏差上限；
- 行为超出签名 Release Package。

## 11. 关键路径与并行策略

```text
G0 授权
-> 合约和时间语义
-> 不可回填数据前瞻采集
-> 不可变原始事实
-> 数据资格
-> PIT 数据集
-> 统一执行仿真
-> 预注册研究
-> 合格候选
-> paper 校准
-> 冻结前向观察
-> ACK/readback + fault matrix
-> 独立审查
-> 人工 canary 决策
```

不可用增加人员压缩：

- 本地 receive time、延迟、断流和丢包只能从现在采集；
- paper order 和不同 regime 需要自然发生；
- forward observation 必须经过真实日历；
- holdout 失败不能重开；
- 没有候选不能跳到资本链路。

可并行：

- G1 领域合同冻结后，采集、存储和仿真黄金场景可并行；
- G2 期间可开发 G3 的确定性测试集；
- G3/G4 期间可构建 G7 的故障 harness；
- UI 只在 API 和证据合同冻结后并行；
- 第二来源可作为交叉验证并行，但不阻塞单主来源纵向切片。

## 12. 第一个 30/60/90 日执行周期

### 前 30 日：合同与前瞻事实

交付：

1. 建立新代码库、独立 CI、权限域和证据目录；
2. 完成 Program Charter、RACI、Threat Model、Legacy Freeze Policy；
3. 完成 Instrument Master v0、Canonical Event v0、Dataset Contract v0；
4. 建立 BTC/ETH 永续黄金合约和事件样例；
5. 启动不可回填的 receive time、generation、drop、latency 前瞻采集；
6. 跑通 raw object -> manifest -> checksum -> restore；
7. 完成一日、七日和峰值五倍突发容量基准；
8. 完成数据许可和保留政策。

30 日门：没有 unresolved 的合约单位、时间语义、权限边界或数据许可；恢复演练通过；否则停止扩建。

### 31--60 日：PIT 纵向切片

交付：

1. raw -> canonical 的确定性转换；
2. quality/gap/quarantine；
3. 版本化 Dataset Snapshot；
4. 第一组 Feature/Label Definition；
5. `available_time` as-of join；
6. future-leak 攻击测试；
7. 从快照重建同一 feature fingerprint；
8. 第二来源的独立交叉检查原型。

60 日门：同一输入跨隔离环境可复现；任何未来数据攻击不得逃逸。

### 61--90 日：第一条研究证据链

交付：

1. 确定性 bar/trade replay 最小内核；
2. fee、funding、基础 margin 和订单状态机；
3. Hypothesis Card、Protocol、Trial Ledger；
4. 一项预注册机制的完整失败/通过证据；
5. Evidence Bundle fingerprint；
6. 独立 worker 复现；
7. UI 只实现 Data Truth、Experiment Evidence 两个只读视图；
8. G0/G1 正式评审和 G2/G3 差距报告。

90 日门：必须跑通：

```text
raw -> canonical -> PIT feature -> deterministic replay
-> complete trial ledger -> evidence fingerprint -> independent reproduce
```

该门通过不代表有盈利候选，也不授权 paper 参数应用或 live。

## 13. 团队、职责与资源模式

### 13.1 推荐角色

| 角色 | 推荐配置 | 决策责任 |
| --- | ---: | --- |
| Program Owner | 1 | 目标、预算和范围；不能单独批准候选 |
| Principal Quant | 1--2 | 假设、统计、试验预算 |
| Market Data Engineer | 1--2 | 采集、语义、质量、恢复 |
| Execution/Simulation Engineer | 1--2 | 订单、L2、费用、校准 |
| Platform Engineer | 1--2 | 存储、元数据、任务、API、证据 |
| SRE/Security | 0.5--1 | 隔离、可靠性、故障和供应链 |
| UX Engineer | 0.5 | 证据工作台 |
| Independent Model-Risk Reviewer | 0.5--1 | 独立复现与否决 |
| Legal/Data Licensing | 按需 | 数据和交易所条款 |

机构级推荐有效规模为 8--10 FTE。G0 已选择 Owner-led、AI-assisted 的“精简专业模式”：
G0--G4 计划 24 FTE-month、硬上限 29 FTE-month，最低需要 Data/Platform、Execution/Simulation、
SRE/Security 和独立 Model-Risk 的明确人类能力。单一负责人配合 AI 可以完成大量工程建设，但 AI
不能形成独立性；没有实名 Independent Reviewer 时，G0 不能关闭，G1--G4 不能正式过门，也不能
进入 G5--G8。AI 能缩短实现时间，不能缩短前瞻采集、paper 样本和 forward observation 的日历时间。

### 13.2 决策权

- Quant 可以提出候选，不能批准自己开发的候选；
- Risk 和 Independent Reviewer 拥有否决权；
- Operator 只能执行已签名、未过期且 Runtime 校验通过的决定；
- AI 可以研究、实现和整理证据，不能打开 holdout、批准资本、签名、部署、恢复 live 或解除
  Kill Switch；
- 无法提供独立风险审查时，只能继续 G0 允许的隔离、可逆 bootstrap；G0 不能关闭，G1--G4
  不能正式过门，更不能进入 G5--G8。

### 13.3 预算方法

G0 已在 [`G0 立项决议记录`](aats_research_os_g0_decision_record_2026_08_26.md) 形成预算边界：
G0--G4 直接现金硬上限 USD 120,000，其中基础阶段 USD 96,000、25% 不确定性储备 USD 24,000；
现有人员工资、新增长期工程人员和交易资本不在该口径内。若需新增人员，必须重新打开资源和预算
决策。预算台账至少分为：

- 人员；
- 数据许可；
- hot/warm/cold 存储和双副本；
- 回放计算；
- paper/影子环境；
- CI、监控、备份和灾难恢复；
- 安全审计与法律；
- 25% 不确定性储备；
- Canary 资本单独审批，不计入平台建设费用。

资源投资建议：30% 数据事实、25% 执行数字孪生、20% 科学研究、15% 可靠性/安全/资本治理、
10% 产品界面与开发体验。没有正候选时，不把预算转向大规模 UI 或无门槛扩大技术栈。

## 14. 可靠性、安全与可复现性

### 14.1 可靠性不变量

- 采集采用至少一次投递与幂等去重，不宣称不可证明的 end-to-end exactly-once；
- 本地持久 spool/WAL 先于下游派生；
- 任务具备幂等键、lease、heartbeat、retry budget 和 dead-letter 状态；
- 原始采集优先级高于特征和实验；
- 质量异常进入 quarantine，不静默变成空数据；
- 时钟偏移与同步状态进入数据质量；
- 健康、可服务、数据新鲜、研究可信和资本可发布是五种不同状态；
- 元数据、对象、签名和发布包定期做恢复演练。

### 14.2 初始 SLO

以下是新平台目标，不是当前实现事实：

- 原始对象 checksum/manifest 覆盖率 100%；
- 晋级证据 lineage 完整率 100%；
- 资本资格窗口未分类缺口 0；
- 数据静默覆盖 0；
- 已晋级 Evidence Bundle 独立复现率 100%；
- 未签名/过期 Release Package 被接受次数 0；
- Research OS 直接写 Runtime 次数 0；
- 采集以实测峰值至少做五倍突发验证；
- 元数据 RPO 目标不高于 5 分钟、RTO 目标不高于 1 小时；
- 原始对象至少两个故障域副本，季度恢复演练。

具体延迟、吞吐和可用性 SLO 在 30 日基准后冻结，不能在没有测量时制造精确数字。

### 14.3 可复现性

每个实验锁定源码 commit/tree、OCI digest、依赖 lock、数据快照、Instrument Master、特征图、
仿真模型、canonical config、seed 和 CPU/浮点兼容信息。

必须区分：

- Decimal、整数、订单和账务结果严格相同；
- 浮点统计在预注册容差内相同；
- 非确定性算法必须声明 seed、并行归约和硬件边界。

### 14.4 安全

- 最小权限服务身份和服务间认证；
- Secret 只进入 Secret Store，不进入配置、日志或 artifact；
- raw、research、release、feedback 分权；
- 普通研究身份与发布签名身份分离；
- 审计 append-only/WORM；
- OCI、依赖、SBOM 和 Release Package 做供应链校验；
- AI 代理视为不受信任的高能力调用者；
- 任何 Research 到 Runtime 的写路径均为安全缺陷。

## 15. 测试与验证战略

| 层级 | 必须验证 |
| --- | --- |
| Schema/Contract | 兼容、版本、未知字段失败、幂等键冲突、hash 篡改 |
| 金融属性测试 | 数量/名义价值、fee sign、PnL、funding、margin、liquidation 恒等式 |
| 时间攻击测试 | future join、late correction、同时间事件排序、bar causality、clock skew |
| 数据故障 | disconnect、drop、duplicate、乱序、截断文件、坏 checksum、磁盘低水位 |
| 可复现性 | 跨进程、跨机器、重试、恢复、并行归约、artifact fingerprint |
| 仿真 | partial/no-fill、撤单竞态、拒单、限频、重启、不同 book 层级 |
| 差分验证 | Legacy 与新系统相同输入的事件数、单位、特征、决策差异及解释 |
| 安全 | Research 无 Runtime 写权限、签名篡改、过期包、部分 ACK、权限到期 |
| 性能 | 峰值五倍采集、30/90 日扫描、L2 回放、并发实验和资源隔离 |
| 恢复 | object/metadata restore、RPO/RTO、孤儿任务、断点恢复和重复执行 |
| Paper/Shadow | prediction-order-fill-PnL 绑定、执行误差和 drift |

阶段验证必须区分：静态、单元、集成、故障注入、受控模拟、日历观察和独立复核。任何未执行项
保持 `UNKNOWN`，不能用低层验证替代高层结论。

## 16. KPI、反指标与阶段报告

### 16.1 正确 KPI

| 目标 | KPI |
| --- | --- |
| 市场事实可重建 | raw manifest/checksum 100%；未分类缺口 0；恢复 100% |
| 研究可证伪 | Trial 登记 100%；未授权 holdout 访问 0；独立复现 100% |
| 无时间泄漏 | PIT 攻击逃逸 0；offline/shadow parity 在容差内 |
| 仿真有预测力 | 生命周期 100%；fill/价格/费用/时延在预注册包络内 |
| 资本失败关闭 | 未授权 live 0；ACK/readback 不一致时成功发布 0；不可归因订单 0 |
| 提高经济价值 | 降低证伪时间；候选成本后为正；容量和回撤满足预算 |

### 16.2 反指标

以下不能作为成功指标：

- 代码量、表数量、服务数量；
- 采集字段和交易所数量；
- 回测、trial、candidate 或 workflow 完成数量；
- 毛收益、最优 Sharpe、最优参数；
- HTTP 200、容器 healthy 或任务退出码 0；
- UI 页面数量和视觉完成度。

### 16.3 报告必须回答

每次阶段报告必须回答：发生了什么、依据是什么、什么仍未知、为什么可以/不可以进入下一门、
成本与风险是否改变、回退点在哪里。不得只列“完成任务”。

## 17. 风险登记与停止条件

| 风险 | 领先信号 | 缓解 | 停止触发 |
| --- | --- | --- | --- |
| 合约单位错误 | venue 间 notional/OI 不一致 | Instrument Master、黄金样例、双来源 | 金融恒等式失败 |
| 时间泄漏 | 回测异常优秀、PIT 攻击失败 | available_time、as-of join | 任一未解释泄漏 |
| 缺口被伪装 | 历史回填覆盖本地断流 | source contract、quarantine | lineage 混用 |
| 数据规模失控 | 存储/扫描成本快速上升 | 对象存储、列式、retention | 超预算或安全水位 |
| 过拟合 | trial 增长、反复改窗口 | 预注册、预算、DSR/Holm、holdout | 预算耗尽 |
| 仿真偏差 | fill/slippage drift | paper 绑定、分层校准 | 超包络 |
| 单交易所依赖 | API/规则变化 | 版本化适配器、第二来源 | 语义无法证明 |
| 过度工程 | 组件增长快于研究能力 | benchmark 后引入 | 连续两阶段无价值提升 |
| Legacy 污染 | 新系统读取旧 mutable truth | Anti-Corruption Layer | 出现共享写入 |
| 权限泄漏 | 异常访问、审计断链 | 最小权限、独立密钥 | 任一安全事件 |
| 团队单点 | 仅一人理解核心领域 | 双人 ownership、演练 | 无独立审核能力 |
| 许可风险 | 数据保存权不清 | Legal review、许可登记 | 未获使用权 |
| 机制衰减 | regime 敏感、前向失效 | Challenger、退役规则 | 失效门触发 |
| AI 错误 | 未复核代码/研究 | 隔离、独立复现 | AI 改变资本状态 |

整体暂停条件：

- 数据许可不明；
- 无法实现 Research/Runtime 权限隔离；
- 核心单位或时间语义无法冻结；
- 成本超预算 25% 且无经批准的降本方案；
- 凭证、安全或审计事件；
- 无独立审核人却试图进入资本阶段；
- 团队以日期或业务压力替代证据门。

机制族终止条件：Trial Budget 用完、成本后 edge 消失、依赖 Runtime 不可得数据、主要收益来自
极少异常窗口、统计或 paper 失败、holdout 失败、容量不足、失效条件无法在线观测。

## 18. Legacy 迁移与退役

1. **冻结**：旧 RDP 只接受安全、连续采集、严重正确性和合规修复；
2. **清点**：按保留证据、转换迁移、只读包装、淘汰分类；
3. **隔离**：旧 mutable 表不得直接成为新 canonical truth；
4. **Crosswalk**：维护旧 ID、时间、单位、source、lineage、参数的映射；
5. **双跑**：对相同公共数据比较事件、单位、特征和研究差异；
6. **影子**：新平台不得写参数或订单；
7. **契约集成**：AATS 只消费签名 Release Package；
8. **能力级切换**：依次切事实、PIT、回放、研究、候选和发布；
9. **回退**：任何阶段只能退回新系统只读/影子，不退回旧 RDP 自动应用参数；
10. **封存**：证据、crosswalk、恢复和审计完成后，Legacy 进入只读归档；
11. **删除**：只有引用、法律保留、恢复和审计均明确后，另行提出删除计划。

## 19. 治理节奏与签字

- 每日：数据新鲜、缺口、延迟、drop、质量和成本；
- 每周：项目 WBS、依赖、风险、资源；
- 每周：研究证伪会，只审机制、失败、Trial Budget，不讨论降低门槛；
- 双周：架构、ADR、技术引入和容量；
- 每月：Alpha/Risk Committee，审候选、holdout、执行偏差和资本资格；
- 每阶段：只有 Evidence Bundle 完整才召开 Gate Review；
- 每季度：独立安全、模型风险、许可和恢复审查；
- 事件后：先停止，再调查；恢复需人工批准。

最小签字：

- G1--G4：领域 Owner + Independent Reviewer；
- G5--G6：Quant + Execution + Risk；
- G7：Security/SRE + Risk + Independent Reviewer；
- G8：Program Owner + Risk + Operator 双签，并另行明确资金授权。

## 20. 初始 ADR 清单

| ADR | 初始决策 | 代价 | 重评条件 |
| --- | --- | --- | --- |
| ADR-001 | raw/大数据进对象存储，Postgres 只存元数据与治理事实 | 两种存储 | 长期数据量很小 |
| ADR-002 | 强制四类时间与双时间模型 | 开发复杂 | 只优化实现，不取消 |
| ADR-003 | raw append-only，修订用新事件 | 最新查询更复杂 | 不取消 |
| ADR-004 | Evidence Bundle 内容寻址 | manifest 严格 | hash/规范升级 |
| ADR-005 | 至少一次 + 幂等，不声称 exactly-once | 消费者处理重复 | 原则长期保留 |
| ADR-006 | Python 优先；性能证明后迁 Rust | 后期迁移成本 | 基准证明 Python 不足 |
| ADR-007 | 模块化单体起步，按负载拆分 | 故障域较粗 | 团队/吞吐/发布冲突 |
| ADR-008 | 独立部署，Runtime 主动拉取签名包 | 发布更慢 | 安全边界不取消 |
| ADR-009 | 共享纯模型包，不共享服务/数据库 | 包治理成本 | 不用源码复制规避 |
| ADR-010 | 窄而正确的 PIT Feature Graph | 初期功能少 | 多在线消费者出现 |
| ADR-011 | 假设驱动新增数据 | 探索范围较窄 | 单列探索预算 |
| ADR-012 | 一次性 HoldoutClaim | 研究更慢 | 失败后建新代 holdout |
| ADR-013 | 不用隐蔽总分选择“最优” | 不能一键决策 | 效用函数有长期校准后 |
| ADR-014 | AI 无资本发布权 | 自治程度较低 | 长期独立审计后另议 |

## 21. 计划完成定义

只有同时满足以下条件，才可以说 Research OS 平台建设完成：

- 任一市场事件可追溯到 raw object、capture generation、source 和 InstrumentVersion；
- 可以重现历史当时可见状态，而不只是今天修订后的历史；
- 任一特征可回答定义、单位、窗口、available time、版本、依赖和消费者；
- 任一候选可反查全部成功和失败试验；
- Evidence Bundle 可在隔离环境复现；
- 仿真与 Runtime 使用同一版本化金融、订单和策略语义；
- paper/shadow 可逐订单校准仿真；
- Research OS 技术上不能直接下单、写 Runtime 参数或解除 Kill Switch；
- Runtime 不接受未签名、过期、不完整或风险不兼容的发布包；
- 无合格候选时，系统稳定输出 `NO RELEASE`。

即使全部满足，也只证明平台具备机构级研究与资本交付能力，不证明未来高盈利。资本成功还必须由
真实候选、前向证据和受控资本结果持续证明。

## 22. G0 决议与下一授权点

2026-08-26 的 [`G0 立项决议`](aats_research_os_g0_decision_record_2026_08_26.md) 已确认：

1. 建立独立 `AATSResearchOS` 仓库和独立运行域；
2. 首个楔子为 OKX BTC/ETH USDT 线性永续，5 分钟信号、15 分钟至 4 小时研究持有期；
3. 项目发起人担任 Program Owner，领域角色可在早期暂代，但独立人类 `IRR-01` 不可替代；
4. 采用 Owner-led、AI-assisted 的精简专业模式；
5. Binance USD-M 为有条件批准的第二只读验证源，Bybit 为需书面许可的备选；
6. G0--G4 直接现金硬上限 USD 120,000，计划 24、硬上限 29 FTE-month；
7. Legacy Freeze 立即生效。

项目启动状态为 `APPROVED_WITH_CONDITIONS`，但 G0 Gate 仍为 `OPEN / NOT PASSED`。下一步执行
[`G0 收口与 G1 启动计划`](aats_research_os_g0_closure_and_g1_kickoff_plan_2026_08_26.md)，补齐独立
Reviewer、Owner 私有责任册、数据许可、威胁模型、预算台账和 Legacy Freeze 证据。只有 G0 Closure
Packet 获 PO 与独立 Reviewer 共同签字，才解锁正式 G1 和 G1 预算。任何 live、真实资金或 Runtime
参数写入仍需要更晚的独立授权，本次决议不包含该权限。

## 23. 外部参考与借鉴边界

- [QuantConnect Research Pipeline](https://www.quantconnect.com/docs/v2/cloud-platform/research-pipeline)：
  借鉴 Idea、Research、Backtest、Paper、Live 的显式阶段与纸面偏差验证；不复制其云 IDE 和计费系统。
- [NautilusTrader 文档](https://nautilustrader.io/docs/latest/) 与
  [Backtesting](https://nautilustrader.io/docs/latest/concepts/backtesting/)：借鉴研究、回放和 live 共用
  事件与执行语义；不直接替换 AATS Runtime。
- [Feast Point-in-time Joins](https://docs.feast.dev/getting-started/concepts/point-in-time-joins)：
  借鉴 PIT correctness 和历史可见状态；首期不必部署完整 Feast。
- [Apache Iceberg Branching and Tagging](https://iceberg.apache.org/docs/latest/branching/)：
  借鉴 snapshot、time-travel 和 write-audit-publish；在规模门触发后再引入。
- [OpenLineage Run Cycle](https://openlineage.io/docs/spec/run-cycle/)：借鉴 Dataset、Job、Run 和
  START/RUNNING/COMPLETE/ABORT/FAIL 血缘事件；新平台可兼容规范，不必首期部署完整后端。

这些资料用于验证设计原则，不构成供应商选择，也不覆盖 AATS 的资金、权限和失败关闭约束。
