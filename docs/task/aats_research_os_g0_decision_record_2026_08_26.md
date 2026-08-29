# AATS Research OS G0 立项决议记录

> **2026-08-29 局部决策失效说明**：本文关于 Binance 作为第二来源、许可对象或 G1 探针的决定已被
> [`AATS Research OS 来源准入与收益优先级纠偏`](aats_research_os_source_access_and_profit_priority_correction_2026_08_29.md)
> 替代。Binance 不再属于现行架构、许可范围或交付前提；其余 G0 人类责任、预算、信任根和 Gate
> 边界仍按本文及独立仓库当前治理证据判断。本文保留原文，只用于追溯当时决策。

> 文档状态：现行项目决议；项目启动 `APPROVED_WITH_CONDITIONS`，G0 证据门 `OPEN / NOT PASSED`
> 决策日期：2026-08-26
> 最后核对：2026-08-27（AATS 代码基线 `40dc6817861a1ddfd92cc8a01d2b9ce87af523aa`，分支 `main`）
> 决策依据：当前静态代码、[`AATS Research OS 独立建设计划书`](aats_research_os_program_plan_2026_08_26.md)、官方交易所接口与条款资料、独立治理反证
> 运行边界：本次未读取当前容器、数据库、采集覆盖、账户、仓位、订单、Kill Switch 或 active parameter 状态
> 授权边界：本决议不授权 live、不授权真实资金、不授权下单、不授权 Runtime 参数写入，也不构成任何收益保证

## 0. 决议结论

本次 G0 决策已经完成，但必须把“同意启动计划”和“已经通过 G0 证据门”分开：

| 事项 | 决议 | 当前状态 |
| --- | --- | --- |
| D01 独立仓库与隔离 | 建立独立 `AATSResearchOS` 仓库及独立运行域 | 批准 |
| D02 首个研究楔子 | OKX BTC/ETH USDT 线性永续，5 分钟信号、15 分钟至 4 小时研究持有期 | 批准 |
| D03 Owner 与 Risk Reviewer | 项目发起人作为 Program Owner 候选意向；所有 Owner/Reviewer 均须在私有责任册实名接受并验证 | 条件性批准；当前未形成任命事实 |
| D04 资源模式 | Owner-led、AI-assisted 的“精简专业模式” | 批准，实际人员能力待登记 |
| D05 第二数据源 | Binance USDⓈ-M Futures 作为第二只读验证源；Bybit 仅作需书面许可的备选 | 条件性批准 |
| D06 G0--G4 预算 | 直接现金硬上限 USD 120,000；计划投入 24 FTE-month、硬上限 29 FTE-month | 批准 |
| D07 Legacy Freeze | 现有 RDP 立即停止新增重复研究能力，仅保留高严重度修复和不可回填采集连续性 | 批准并立即生效 |
| D08 G0 Gate | 条件未关闭前只允许 W0 收口及可逆 bootstrap | `OPEN / NOT PASSED` |

因此，本次会议不是 `NO-GO`，也不是无条件 `GO`，而是：

> 可以建立独立仓库、完成治理收口和无凭证骨架；在独立人类风险审核人、数据许可、责任接受和隔离证据全部关闭前，不得宣称 G0 已通过，不得进入正式 G1。

### 0.1 备选方案与取舍

| 决策 | 已选择 | 未选择及原因 | 重评触发 |
| --- | --- | --- | --- |
| 仓库 | 独立 `AATSResearchOS` | 原地 RDP V4 或主仓子目录会继续共享权限、发布和可变真源 | 只可重评物理实现，不取消写隔离 |
| 楔子 | OKX BTC/ETH 线性永续、15m--4h | 首日多市场、多币种或 HFT 会在单位、时间和执行真值未稳定前放大错误 | 首个纵向证据链稳定且有新增假设 |
| 资源 | 精简专业模式 | 单人模式没有独立审核；8--10 FTE 机构模式在价值未证明前固定成本过高 | 连续两个 gate 由人员容量阻塞 |
| 第二来源 | Binance USD-M 条件性通过 | Bybit 条款/地域风险更高；商业供应商在公共源许可失败前暂不采购 | 许可失败、覆盖不足或连续性不达标 |
| 预算 | USD 120,000 直接现金硬上限 | USD 30k--50k 只能支撑短原型，无法可靠覆盖 G0--G4 的外部审核、许可、恢复与执行验证；机构级投入暂不前置 | 新增长期人员、商业数据或阶段预测超上限 |

## 1. 决策权与参会角色

### 1.1 决策权

- `PO-01`：AATS 项目发起人是 Program Owner 的设计意向人选；在私有责任册完成实名接受、
  commitment、key fingerprint 和有效期验证前，不构成已任命事实；
- Codex：会议组织、证据整理、计划编写和后续实施支持；不是法律主体、风险签字人或资本批准人；
- 独立审查意见：用于反证决策，但不同 AI 代理之间的互审不构成独立人类审核。

### 1.2 G0 为什么仍未过门

当前尚未形成以下两项可验证事实：

1. `IRR-01` 独立人类 Model-Risk Reviewer 已实名接受任命、披露冲突并接受否决权；
2. OKX 与 Binance 的部署地域、采集、保存、派生研究和内部使用权已经由人类责任人形成书面许可结论。

这两项都是计划书已有的 G0 硬退出门。项目管理上的批准不能覆盖证据门，因此 G0 状态必须保持开放。

## 2. D01：独立仓库与系统边界

### 2.1 已批准决定

- 代码仓库名：`AATSResearchOS`；Python 包命名空间：`aats_research_os`；
- 新仓库使用独立 Git 历史、CI、依赖锁、制品、数据库、对象存储命名空间、服务身份、审计日志和部署单元；
- 新仓库不得继承或复制 AATS 的 live secrets、交易所下单密钥、Runtime 数据库写凭证或管理 API 凭证；
- Research OS 只能以只读来源取得受批准数据，并输出内容寻址、可签名的 Candidate Release Package；
- AATS Runtime 以后只能通过受控、单向、另行批准的拉取协议消费发布包；不得共享可写数据库或可变目录真源；
- 共享金融语义只能通过版本化、可校验的纯模型包实现，不能用源码复制或跨库直写替代。

### 2.2 未授权事项

本次只确认仓库和边界，不表示仓库已经创建，也不授权：

- 创建任何 live/service-account 凭证；
- 连接 AATS Runtime 写端；
- 导入 Legacy mutable 表并将其标记为新 canonical truth；
- 部署持续运行服务；
- 创建真实资金、下单或参数应用路径。

### 2.3 重评条件

只有在独立仓库、数据库、对象命名空间和服务身份造成了已量化且无法接受的维护成本时，才可重评物理实现；“研究与 Runtime 无共享写权限”的边界不可取消。

## 3. D02：首个 BTC/ETH 永续楔子

### 3.1 市场边界

| 维度 | G0 决定 |
| --- | --- |
| 目标交易所语义 | OKX public/history；以 OKX 合约规则作为目标执行语义 |
| 主产品 | `BTC-USDT-SWAP`、`ETH-USDT-SWAP` |
| 合约类型 | USDT 保证金线性永续；不把币本位、交割、期权混入首期 |
| 研究决策频率 | 5 分钟 |
| 主要持有期 | 15 分钟、1 小时、4 小时；5 分钟仅用于短周期诊断 |
| 研究用途 | 隔离回放、研究和后续纸面验证；G0--G4 不进行真实交易 |
| 明确排除 | 微秒级 HFT、共址做市、跨保证金组合、杠杆优化、自动资本扩张 |

### 3.2 初始研究假设边界

G0 只批准市场边界，不批准任何候选。G4 前最多预注册两个机制族：

1. funding、OI、订单流和流动性状态共同驱动的短周期方向/风险过滤；
2. OKX 与合格第二来源之间的 lead-lag 或价格发现差异。

第二来源在 G1--G2 首先是事实交叉核验源；一旦被用作特征输入，就必须升级为完整受治理主数据源，满足与 OKX 相同的合同、血缘、许可、缺口和时间语义要求。

### 3.3 成功定义

首个楔子的工程成功是形成可重建、无未来泄漏、可复现并可校准的证据链；是否存在成本后正期望只能在 G5 以后由预注册研究、封存 holdout 和前向观察回答。

## 4. D03：Owner、独立 Reviewer 与否决权

### 4.1 角色设计意向（不是任命事实）

以下表格记录 G0 决策时的责任设计，不覆盖独立仓库
`governance/roles/public_role_registry.json` 的当前状态。2026-08-27 的公开真值仍为：
PO/DO/QO/EO/PE/SEC `UNVERIFIED`，IRR/DLR `UNASSIGNED`；私有接受、实名和签字均未验证。

| 角色 | 当前任命 | 建设期可否暂代 | 硬边界 |
| --- | --- | --- | --- |
| Program Owner `PO-01` | 意向人选：AATS 项目发起人；未验证接受 | 仅是设计意向 | 管目标、范围和预算；不能单独批准 gate 或候选 |
| Data Owner `DO-01` | 未任命；不得把 PO 意向自动视为暂代接受 | 需私有责任册明确 | 管数据语义、质量、许可、恢复；不能自审 |
| Quant Owner `QO-01` | 未任命；不得把 PO 意向自动视为暂代接受 | 需私有责任册明确 | 管假设、统计、Trial Budget 和淘汰规则；不能自批候选 |
| Execution Owner `EO-01` | 未任命 | 需私有责任册明确 | 管订单、fee、funding、margin、PnL、fill 和容量语义 |
| Platform Owner `PE-01` | 未任命 | 需私有责任册明确 | 管存储、CI、任务、恢复、成本和制品完整性 |
| Independent Model-Risk Reviewer `IRR-01` | 席位已批准；实名人选未任命 | 不可以 | 独立复现、有效挑战、单方否决 |
| Data Licensing/Legal Reviewer `DLR-01` | 按需外聘；实名人选未任命 | 不可以由 AI 代替 | 对采集、保存、派生、内部使用和再分发权承担人类责任 |
| Security/SRE Reviewer `SEC-01` | 未任命；可兼职的设计不等于已接受 | 有限且需实名验证 | 管隔离、供应链、恢复和故障证据 |
| Codex/AI Delivery Agent | 实施支持 | 可以执行 R 类工作 | 不得成为 A、V 或签字人 |

“暂代”只解决责任归属，不产生独立性。由同一负责人或 AI 生成的产物，仍必须由 `IRR-01` 独立审核。

### 4.2 不可兼任规则

- `IRR-01` 不得兼任 PO/DO/QO/EO/PE/SEC/DLR 中的任何角色；
- `IRR-01` 不得作为产物 Owner 预批准，也不得参与本次 Gate 证据的形成、实施或修改，包括代码、
  合同、参数、数据修订、Threat Model、License Matrix 或候选筛选；证据冻结后，IRR 必须独立
  执行 `ACCEPT / REJECT / REMEDIATE` 审查并签署 Gate 结论；
- 同一开发者、同一 Codex 会话或不同 AI 代理之间的互审不构成独立审核；
- Program Owner 不能覆盖 Risk/Reviewer 的否决；否决后只能整改并提交新版本；
- Reviewer 报酬不得与候选通过、收益表现或上线速度挂钩；
- Codex 可以整理材料、执行测试和复现，不能打开 holdout、签字、批准资本、解除 Kill Switch 或恢复 live。

### 4.3 RACI 与签字

`V` 表示不可被覆盖的否决权，`R-support` 表示 Codex 只负责实施支持。

| 决策或交付 | PO | Data | Quant | Execution | IRR | Legal | Codex |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 目标、范围、预算 | A | C | C | C | C/V | C | R-support |
| 仓库与 Runtime 隔离 | A | C | I | C | V | I | R |
| 数据源与合同 | A | R | C | C | V | R/C | R-support |
| Instrument/Canonical Event | I | A/R | C | C | V | I | R |
| PIT 特征和标签 | I | A/C | A/R | C | V | I | R |
| Hypothesis/Trial Budget | I | C | A/R | C | V | I | R-support |
| Execution Twin | I | C | C | A/R | V | I | R |
| Gate Review | C | 领域签字 | 领域签字 | 领域签字 | 必签/V | 按范围必签 | 记录，不签字 |

每次签字必须绑定 gate、scope、commit/tree digest、data manifest digest、contract/config/model digest、风险边界、例外、签字角色、时间和有效期。若同一人暂代多个角色，也必须分别以每个角色作责任声明。最低签字规则：

- G0：PO、DO、QO、EO、PE、IRR 和 DLR；C04 Threat Model 另需 SEC 签字；
- G1：Data Owner + IRR；
- G2：Data Owner + Quant Owner + IRR；
- G3：Execution Owner + Quant Owner + IRR；
- G4：Quant Owner + IRR，且 Data/Execution Owner 对引用合同 digest 作不变性声明。

任一必签缺失、证据变化、签字过期或条件未关闭，状态只能是 `NO-GO`。

## 5. D04：精简专业资源模式

### 5.1 已选择模式

采用 `Lean Professional / 精简专业模式`，不是单人随意开发，也不在 G0 即扩张为 8--10 FTE 机构团队：

| 能力 | G0--G4 最低可用容量 |
| --- | ---: |
| Program/Data/Quant 责任 | 1.0 human FTE，可在早期合并角色 |
| Data/Platform Engineering | 1.0 human FTE |
| Execution/Simulation Engineering | 0.5--1.0 human FTE，G3 应达到 1.0 |
| SRE/Security | 0.2--0.3 human FTE |
| Independent Model Risk | 0.2--0.3 human FTE，必须独立 |
| Legal/Data Licensing | G0--G1 预留 5--10 个专业咨询日，之后按需 |
| Codex/AI | 自动化实现、测试和证据整理；不计作独立人类 FTE |

计划容量是 24 FTE-month，硬上限 29 FTE-month。按 3 个有效人类 FTE 计算约为 8 个日历月；若只有一个人承担全部可合并角色，日历时间将显著延长，不能通过降低 gate 或把 AI 当签字人来压缩。

### 5.2 扩容触发

只有出现以下证据之一才申请扩容：

- 单机或现有人员成为连续两个里程碑的主瓶颈；
- 独立复现、数据恢复或执行校准因职责冲突无法完成；
- 已有一个合格机制族，需要并行推进而不会扩大无约束搜索；
- 安全、许可或审计工作超出兼职能力。

## 6. D05：第二数据源

### 6.1 决定

将 Binance USDⓈ-M Futures 的 `BTCUSDT`、`ETHUSDT` 永续确定为 OKX 的第二只读验证源，状态为 `APPROVED_WITH_CONDITIONS`。Bybit V5 不进入当前接入清单，只保留为取得书面许可后的备选。

技术依据：Binance 官方 USD-M 接口覆盖公开 trades、diff book、BBO、mark/index、funding 和 OI；公共历史仓库提供部分日/月数据与校验信息。Binance 普通 depth 不包含 RPI，历史接口也不能被解释为完整历史 L2，因此盘口必须前瞻采集并明确可见性边界。Bybit 的盘口层级和 `u/seq/cts` 语义具有吸引力，但其当前 API 条款和地域边界需要更严格的书面许可判断。

官方核对入口：

- [Binance USD-M WebSocket](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect)
- [Binance WebSocket 路由迁移与 Stream Mapping](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice)
- [Binance 本地订单簿构建规则](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly)
- [Binance USD-M REST 市场数据](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [Binance 公共历史数据说明](https://github.com/binance/binance-public-data/blob/master/README.md)
- [Binance Terms](https://www.binance.com/en/terms)
- [Bybit V5 Orderbook](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)
- [Bybit V5 Trade](https://bybit-exchange.github.io/docs/v5/websocket/public/trade)
- [Bybit V5 Ticker](https://bybit-exchange.github.io/docs/v5/websocket/public/ticker)
- [Bybit Integration Guidance 与地域 API 限制](https://bybit-exchange.github.io/docs/v5/guide)
- [Bybit Service Restricted Countries](https://www.bybit.com/en/help-center/article/Service-Restricted-Countries/trade/spot)
- [Bybit API Terms](https://www.bybit.com/common-static/compliance/legal/BYBIT/df1923006718fbba8ba70d7d762b9866.pdf)

### 6.2 接入前置条件

1. `DLR-01` 对实际部署实体、所在地、出口 IP、内部研究、派生结果、保留和未来商业用途形成书面结论；
2. 第二来源只使用公开市场接口，不创建 API key，不取得账户、下单、转账或提现权限；
3. G0 通过后进行连续 168 小时技术/可用性探针和历史清单审计；连通成功不等于许可通过；
4. 建立 endpoint capability matrix。每个接口声明原生 event/exchange time、sequence/update ID、
   snapshot/delta 和 provider checksum 能力；来源没有提供的字段保持 `null` 并记录原因，禁止合成；
5. raw 必填 endpoint、schema revision、local receive time、`raw_content_digest` 和
   `manifest_digest`；`provider_checksum` 仅在来源实际提供时记录，不能把 Research OS 自算摘要
   冒充来源校验；
6. Binance 历史 L2 不得伪回填；从 G1 起前瞻采集，缺口必须显式；
7. 截至 2026-08-26，`/fapi/v1/openInterest` 只提供当前 OI；
   `/futures/data/openInterestHist` 提供 5m--1d 统计、每页最多 500 条、仅最近一个月。接入前必须
   重新核对；更早 OI 不得声称可由当前 REST 恢复；
8. 若 Binance 条款或地域审查失败，转向正式签约的商业数据供应商；不得绕过地域限制；
9. Bybit 只有取得 Bybit/签约主体对拟议用途的书面同意或商业数据协议，并由 `DLR-01` 对地域和
   用途签字确认后才可升格。内部法律意见不能自行豁免平台条款，也不得绕过美国、中国大陆等
   地域的 API/服务限制。

## 7. D06：G0--G4 预算边界

### 7.1 预算口径

本预算是内部规划硬上限，不是供应商报价、付款承诺或盈利预测。USD 120,000 覆盖 G0--G4 的
全部新增现金流出，包括基础设施、数据、备份、CI、外部模型风险/安全/法律服务、必要硬件、税费、
支付手续费和汇率影响；不包含：

- 项目发起人和已在岗人员的既有工资；
- 新增全职工程人员或长期工程外包；
- paper 保证金、Canary 或任何真实交易资本。

如果需要新增全职人员或长期工程外包，必须在签约前重新打开 D04/D06，并用新的总现金上限替换
本决议；本决议对这类新增人员的授权预算为 USD 0，不能把费用作为 USD 120,000 之外的附加支出。

### 7.2 已批准上限

| 阶段 | 计划人力 | 基础现金上限 | 主要用途 |
| --- | ---: | ---: | --- |
| G0 | 1.5 FTE-month | USD 8,000 | 许可、角色、威胁模型、隔离设计、预算台账 |
| G1 | 6.0 FTE-month | USD 28,000 | 领域合同、raw 存储、采集、质量、恢复 |
| G2 | 4.0 FTE-month | USD 18,000 | PIT、特征、快照、holdout 隔离 |
| G3 | 7.0 FTE-month | USD 28,000 | 事件回放、订单/账户模型、paper 校准基础 |
| G4 | 5.5 FTE-month | USD 14,000 | 研究协议、Trial Ledger、统计裁决和独立复现 |
| 基础合计 | 24.0 FTE-month | USD 96,000 | 不含储备 |
| 不确定性储备 | 最多 5.0 FTE-month | USD 24,000 | 仅经变更记录释放 |
| G0--G4 硬上限 | 29.0 FTE-month | **USD 120,000** | 不得自动超支 |

### 7.3 资金释放与控制

- 当前只确认 G0 的 USD 8,000 规划上限，不自动授权任何外部付款；每项实际采购必须先由 PO
  在预算台账书面批准；G1 预算在 G0 Gate 正式通过前保持锁定；
- G1 前月度经常性成本目标不高于 USD 2,000、硬上限 USD 3,000；G2--G4 目标不高于 USD 4,000、硬上限 USD 6,000；
- 单笔一次性支出超过 USD 2,000、任何新增 recurring 支出超过 USD 500/月，除 PO 台账批准外，
  还必须由受影响领域 Owner 联签；
- 审批阈值按同一供应商、同一目的和滚动 90 日的关联采购合并计算；禁止拆单规避审批；
- 数据许可或单一硬件承诺超过 USD 5,000，必须先有 ADR、容量基准、退出条款和法律/安全意见；
- 任一阶段预计使用达到 80% 时强制重新预测；达到 100% 时阶段停止，不能自动借用下一阶段预算；
- USD 24,000 储备只能由 PO、受影响领域 Owner 和 IRR 对同一变更摘要签字后释放；不得用于
  扩大未经预注册的搜索、装饰性 UI 或绕过失败 gate；税费、支付手续费和汇率缓冲也只能从本
  总额或 reserve 内吸收，不能使实际现金流出突破 USD 120,000；
- 节余不自动扩大下一阶段范围；资本和 live 预算始终为 USD 0，除非未来单独召开资本授权决策。

## 8. D07：Legacy Freeze Policy

自本决议生效，现有 RDP 进入受维护 Legacy 状态。

允许：

- P0 安全、凭证泄露和真实资金风险修复；
- 数据丢失、不可回填采集连续性和恢复修复；
- 金融正确性、审计、合规和当前模拟/只读连续运行所需的严重故障修复。

禁止：

- 新研究 workflow、因子族、推荐或 Runtime 参数应用能力；
- 为旧 RDP 继续扩大研究 schema；
- 非必要 UI 扩建和与 Research OS 重复的架构重构；
- 新增自动 live、下单或 Runtime 写路径。

每个例外必须记录严重级别、证据、最小变更、测试、回退和 sunset。普通例外由 PO 与受影响
Owner 共同批准；涉及证据语义、风险放宽或 Runtime 权限时，`IRR-01` 必签并有否决权。

在 `IRR-01` 尚未任命时，只有凭证泄露、真实资金危险或数据持续丢失等 P0 可以走应急修复：
必须沿用 AATS 现有安全门，由 PO 与独立人类 Security/Risk 双签，默认保持 live 禁用，只允许最小
止血和恢复，不得新增能力或放宽风险。应急变更须在 IRR 到位后五个工作日内、且最迟在下一次
Gate Review 前完成追认审查；未追认则该 gate 保持 `NO-GO`。

## 9. G0 条件、放行范围与硬阻断

### 9.1 必须关闭的条件

| 条件 | Owner | 关闭证据 |
| --- | --- | --- |
| C01 `IRR-01` 实名任命 | PO | 任命、能力说明、冲突披露、保密与否决权接受记录 |
| C02 Owner 责任接受 | PO | DO/QO/EO/PE/SEC 的姓名、能力、时间投入、职责和替代安排写入私有责任册 |
| C03 数据许可 | DO + DLR | OKX/Binance 采集、保存、派生、内部研究、保留和地域结论 |
| C04 隔离与威胁模型 | EO + SEC | 数据流、信任边界、身份、网络、secret 和禁止路径清单 |
| C05 预算治理 | PO | 预算台账、采购阈值、成本告警和停止机制 |
| C06 Legacy Freeze 落地 | PO + DO | 允许/禁止范围、例外模板和现有 backlog 分类 |

### 9.2 条件关闭前允许

- 完成 Charter、RACI、ADR、Threat Model、License Matrix 和私有责任册；
- 预约或创建空的私有仓库；
- 建立无凭证、无第三方持久数据的本地骨架；
- 编写合同草案、合成 fixtures 和金融恒等式测试；
- 评估公开 API schema、限频和条款；
- 任命 Reviewer、取得预算和许可报价。

### 9.3 条件关闭前禁止

- 持久采集第二来源市场数据；
- 建立长期云资源、对象存储或付费订阅；
- 部署持续运行服务或创建 Runtime 互通凭证；
- 把 bootstrap 产物计为 G1 合格证据；
- 导入 Legacy mutable 数据并重新标记真值；
- 构造、批准或发布候选；
- 写 Runtime 参数、启动 live profile 或触发交易。

### 9.4 G0 硬阻断

除 C01--C06 外，出现以下任一事实也必须保持 `NO-GO`：

- 新旧系统共享可写数据库、服务身份、对象命名空间或可变真源；
- 新平台可访问 Runtime 管理 API、下单凭证或 live secrets；
- Reviewer 参与被审范围实现，或其报酬与通过/收益挂钩；
- 预算没有独立审核、许可、安全和 25% 储备；
- 试图用日期、代码量、UI 或任务退出码替代证据门；
- 未定义合约单位、四类时间和来源角色即开始形成研究结论。

## 10. G0 完成定义与下一授权点

本次决策会议的输出已完成；G0 Gate 只有在 C01--C06 全部关闭、签字绑定具体证据摘要且没有硬阻断项时，才可从 `OPEN` 改为 `PASSED`。

下一步执行 [`G0 收口与 G1 启动计划`](aats_research_os_g0_closure_and_g1_kickoff_plan_2026_08_26.md)。目标是在十个工作日内准备 G0 Closure Packet；日期只用于组织，不替代证据。G0 未通过时，正式 G1、持续采集和阶段预算均不释放。
