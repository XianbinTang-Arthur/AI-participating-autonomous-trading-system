# AATS Research OS G0 收口与 G1 启动计划

> **2026-08-29 局部计划失效说明**：本文所有 Binance 许可、能力矩阵、168 小时探针和第二来源交付项
> 已被 [`来源准入与收益优先级纠偏`](aats_research_os_source_access_and_profit_priority_correction_2026_08_29.md)
> 替代。现行 G1 是 OKX-only 技术基线，第二来源为 `UNBOUND`；不得把本文原有 Binance 项目作为 Gate、
> 开发任务或上线前提。本文其余 G0 人类收口要求仍有效。

> 文档状态：已批准的下一步执行计划；G0 Gate 仍为 `OPEN / NOT PASSED`
> 最后核对：2026-08-27（AATS 代码基线 `40dc6817861a1ddfd92cc8a01d2b9ce87af523aa`，分支 `main`）
> 决策来源：[`G0 立项决议记录`](aats_research_os_g0_decision_record_2026_08_26.md)
> 适用范围：G0 条件关闭、独立仓库可逆 bootstrap、G0 通过后的首个 30 日 G1 tranche
> 授权边界：不授权 live、真实资金、下单、Runtime 参数写入、长期第三方采集或资本发布

## 0. 目标与顺序

下一步不是直接全面开发，而是按两个串行授权面推进：

```text
G0 Closure Sprint
-> C01--C06 全部关闭
-> G0 Gate Review = PASSED
-> 解锁 G1 预算
-> G1 30 日 Canonical Truth Kernel tranche
-> G1 证据审查；不因 30 日到期自动过门
```

本计划追求最短的可信闭环，不追求功能数量。任何工作若无法产生可复核证据，或扩大 live/权限边界，应从当前 tranche 移除。

## 1. Now：G0 Closure Sprint

目标工期为十个工作日；这是组织目标，不是 gate 通过承诺。G0 的 USD 8,000 是规划上限，
不自动授权付款；每项实际采购仍须按决议写入预算台账并取得书面批准。

### 1.1 工作清单

| ID | 工作 | Accountable | 依赖 | 必须交付的证据 | 完成条件 |
| --- | --- | --- | --- | --- | --- |
| ROS-G0-001 | 建立私有责任与签字册 | PO | 无 | PO/DO/QO/EO/PE/SEC 姓名、能力、投入、职责、替补和接受时间 | 角色不是空标签，且个人信息不进入公开仓库 |
| ROS-G0-002 | 任命 `IRR-01` | PO | 001 | 资格说明、冲突披露、保密、独立性、固定报酬和否决权接受 | Reviewer 与实现者独立且可执行复现 |
| ROS-G0-003 | 任命 `DLR-01` 并完成 License Matrix | PO + DO | 001 | OKX/Binance 的地域、采集、保存、派生、内部研究、保留和未来商业用途结论 | G1 实际用途最终状态均为 `ALLOW`；只要标签仍为 `CONDITIONAL/UNKNOWN/DENY`，C03 保持 OPEN |
| ROS-G0-004 | 固化隔离 Threat Model | EO + SEC | 001 | 数据流、信任边界、身份、网络、secret、攻击面和明确禁止路径 | Research OS 无 Runtime 写路径、live secret 或共享可写真源 |
| ROS-G0-005 | 建立预算与采购台账 | PO | 001 | G0--G4 阶段上限、已承诺、实际、预测、阈值和 reserve release 流程 | 80% 预警、100% 停止和审批阈值可执行 |
| ROS-G0-006 | 落地 Legacy Freeze | PO + DO | 001 | 当前 RDP backlog 分类、例外模板、Owner、sunset | 新功能冻结，高严重度修复有可审计例外路径 |
| ROS-G0-007 | 预约/创建独立私有仓库骨架 | PE | 004 | 独立 repo/CI/service namespace，secret scan，禁止依赖和 CODEOWNERS 规则 | 无凭证、无第三方持久数据、无 AATS Runtime 写依赖 |
| ROS-G0-008 | 编写核心合同草案与合成 fixtures | DO + EO | 007 | InstrumentVersion、Canonical Event、四类时间、合成黄金样例 | 只用合成数据；未冒充 G1 gate 证据 |
| ROS-G0-009 | 组装 G0 Closure Packet | PO | 002--008 | 决策、条件、签字、许可、威胁模型、预算、freeze 和已知风险的 digest 清单 | PO/DO/QO/EO/PE/IRR/DLR 角色签字齐全；C04 另有 SEC 签字 |
| ROS-G0-010 | 召开 G0 Gate Review | PO + IRR | 009 | PASS/NO-GO 记录、签字、例外、有效期 | C01--C06 全关、最低签字齐全且无硬阻断才可 PASS |

### 1.2 执行顺序

#### 第 1--2 个工作日

- 完成责任册草案和 Reviewer 候选标准；
- 启动 OKX/Binance 许可问题清单；
- 建立预算台账和 Legacy Freeze backlog 视图；
- 固化仓库命名、服务命名和不得继承的凭证/依赖清单。

#### 第 3--5 个工作日

- 完成 `IRR-01`、`DLR-01` 的任命和冲突披露；
- 完成 Threat Model 与数据流图；
- 创建无凭证私有骨架，开启 secret scan、依赖锁和最小 CI；
- 只使用合成数据编写 Instrument/Canonical Event 草案。

#### 第 6--8 个工作日

- 完成数据许可书面结论；
- 完成合成金融恒等式和时间语义 fixtures；
- 审查 Repo/Runtime 隔离，确认没有共享写身份；
- 对预算、角色投入和 Legacy 例外做反证检查。

#### 第 9--10 个工作日

- 生成带 digest 的 G0 Closure Packet；
- 由 `IRR-01` 独立复核，不接受实现者代签；
- 召开 Gate Review，输出 `PASSED` 或带未关闭项的 `NO-GO`。

### 1.3 G0 通过清单

- [ ] `IRR-01` 已实名任命并接受不可覆盖的否决权；
- [ ] DO/QO/EO/PE/SEC 已记录姓名、能力、投入、职责和替代安排；
- [ ] OKX/Binance License Matrix 有人类签字，G1 实际用途最终状态均为 `ALLOW`；
- [ ] 仓库、数据库、对象命名空间、身份和网络边界均独立；
- [ ] Research OS 技术上不能访问下单、Runtime 参数或 live secrets；
- [ ] 预算台账包含独立审核、许可、安全和 25% reserve；
- [ ] Legacy Freeze 已对 backlog 生效；
- [ ] 所有例外、未知项和条件均明确；
- [ ] PO/DO/QO/EO/PE/IRR/DLR 已绑定同一 Closure Packet digest 进行角色签字；C04 另有 SEC 签字。

任一项未勾选，G0 状态保持 `OPEN / NOT PASSED`。

## 2. Next：G1 首个 30 日 tranche

只有 G0 Gate 正式通过后，本节才生效。它是 G1 的第一段交付，不等于 30 日后自动通过完整 G1。

### 2.1 30 日目标

建立 BTC/ETH 永续的 Canonical Truth Kernel 最小纵向切片：

```text
public event
-> append-only raw object
-> manifest + checksum
-> gap/sequence evidence
-> isolated restore
-> deterministic canonical fingerprint
```

该切片不得包含策略选择、候选排名、Runtime 参数或真实交易。

### 2.2 工作包

| 时间窗 | 工作包 | Owner | 交付 | 验收证据 |
| --- | --- | --- | --- | --- |
| Day 1--5 | Repo 与治理内核 | PE + SEC | 独立 CI、依赖锁、SBOM/secret scan、ADR、Run/Evidence 元数据骨架 | 空仓库无 AATS secret；权限矩阵和禁止路径测试通过 |
| Day 1--10 | Instrument Master v0 | DO + EO | OKX/Binance BTC/ETH 版本化合约、数量/面值/tick/lot/fee/funding/margin 定义 | 黄金样例和跨来源单位 crosswalk 全通过 |
| Day 6--12 | Canonical Event Envelope v0 | DO | event/publish/receive/available time、sequence、revision、source、instrument version | schema compatibility、未知字段、重复、乱序和修订测试 |
| Day 8--18 | Immutable Raw v0 | PE + DO | 内容寻址对象、manifest、digest、partition、retention 和 restore | overwrite 被拒绝；随机隔离恢复产生同一 digest |
| Day 12--24 | OKX public 前瞻采集 | DO | BTC/ETH trades/book/funding/OI/mark 的受控只读采集与 continuity ledger | 断流、重连、drop、时钟偏移和 gap 显式可见 |
| Day 15--30 | Binance 168 小时验证探针 | DO + DLR | public-only、无 key 的 endpoint capability/route matrix；`/public` 承载 depth/bookTicker，`/market` 承载 aggTrade/markPrice；OI 使用 REST 轮询 | 连续 168h；两路 WS 独立健康；每路覆盖七次 24h 换线；字段缺失不伪造；地域和条款条件持续满足 |
| Day 18--27 | Raw -> Canonical v0 | DO + EO | 幂等、确定性转换和 quarantine | 相同 raw/config/code 产生相同 fingerprint |
| Day 21--28 | 容量、成本与恢复 | PE + DO + SEC | 1 日、7 日、五倍突发基准；RPO/RTO 初测 | 实测吞吐、存储、恢复、成本与安全水位 |
| Day 29--30 | Tranche Review | DO + IRR；PO 只确认预算 | 证据清单、差距、预算 burn、G1 余项和 CONTINUE/REMEDIATE/STOP | Reviewer 可从 evidence digest 独立抽检；结论不能把完整 G1 标记为 PASSED |

### 2.3 数据范围

首个 tranche 只允许：

- OKX：`BTC-USDT-SWAP`、`ETH-USDT-SWAP`；
- Binance：`BTCUSDT`、`ETHUSDT` USD-M perpetual，只作为只读验证源；
- 事件：Instrument、trade、BBO/diff book、mark/index、funding、OI、collector continuity；
- 时间：按 endpoint capability 保存来源实际提供的 event、exchange publish/transaction，所有记录
  必有 local receive 和 available/persisted；来源未提供的字段保持 `null` 并记录原因，禁止合成；
- 原始 payload 和 canonical event 分离；修订追加，不静默覆盖。

明确不采集账户、订单、余额、私有用户流，不创建 API key，不接入新闻、链上、期权或其他币种。

Binance 探针必须遵守 2026 路由和连接合同：

- depth/bookTicker 使用 `/public`，aggTrade/markPrice 使用 `/market`，两路连接分别监测；
- 单连接只承诺 24 小时有效，必须在到期前建立重叠连接、重新订阅、去重并原子切换；
- depth 换线或 `pu != previous u` 时，按官方规则重新取得 REST snapshot 并重建本地簿；
- 连续窗口为 168 小时。每个通道记录 expected、received、missing、duplicate、out-of-order、
  rebuild、planned/unplanned disconnect、最大 gap 和未分类 gap；
- 对没有 sequence 的 funding/OI REST 轮询，按计划采样数和漏采窗口验收，不能套用 WS sequence；
- 截至 2026-08-26，OI current 使用 `/fapi/v1/openInterest`，历史统计使用
  `/futures/data/openInterestHist`；后者只保留最近一个月、5m--1d、limit 不超过 500，接入前复核。

### 2.4 G1 首 tranche 验收

30 日评审至少需要：

- InstrumentVersion 和金融单位黄金样例全部通过；
- raw 分区的 source、schema、时间边界、`raw_content_digest` 和 `manifest_digest` 覆盖率 100%；
  `provider_checksum` 只在来源实际提供时要求存在；
- 任一 gap、drop、断流、换线、重建或时钟异常都有分类，各通道未分类缺口为 0；
- 隔离恢复不修改源对象，并产生相同 checksum；
- raw -> canonical 在相同 input/code/config 下产生相同 fingerprint；
- Binance 与 OKX 的差异经过证据分类和归因；未解释差异进入 quarantine 并阻断下游结论，
  不得预设为 venue 差异或采集错误；
- 实测容量和月度成本未超过硬上限；
- 没有 Runtime 写路径、live secret 或持久私有数据。

这仍只是 G1 tranche review。完整 G1 退出还需要 [`主计划`](aats_research_os_program_plan_2026_08_26.md) 中全部硬退出门和 `IRR-01` 正式签字。

## 3. Later：G1 之后的依赖顺序

只有前一层证据稳定后才进入下一层：

1. G1 完整通过：Canonical Truth Kernel 与恢复；
2. G2：PIT Dataset、Feature/Label Registry、future-leak attack、sealed holdout；
3. G3：统一订单/账户/fee/funding/margin/liquidation 与 paper 校准；
4. G4：Hypothesis、Trial Budget、统计裁决、失败试验账本和独立复现；
5. G5 以后才讨论候选，G8 仍需新的真实资金人工授权。

## 4. 预算、状态与报告

### 4.1 预算控制

- G0 未通过：只可使用 G0 USD 8,000 上限；G1 USD 28,000 保持锁定；
- G0 Gate 为 PASSED 且 G1 tranche 预算经 PO 书面释放后：月度经常性目标不高于
  USD 2,000，硬上限 USD 3,000；
- 达到阶段预算 80%：停止新增范围，重新预测；达到 100%：停止阶段；
- reserve、付费数据、硬件和新增人员只能按 [`G0 决议`](aats_research_os_g0_decision_record_2026_08_26.md) 的阈值释放。

### 4.2 每周状态格式

每周只报告五类事实：

1. 交付物与 evidence digest；
2. 未关闭条件和 Owner；
3. 预算 actual/forecast/remaining；
4. 数据 continuity、gap、restore 和成本；
5. 风险、停止条件和下周依赖。

不以代码行数、表数、UI 页面或“任务退出码 0”报告成功。

## 5. 停止与升级条件

出现以下任一情况立即停止当前 tranche 并回到 G0/G1 Review：

- Reviewer 独立性失效或关键 Owner 无法履责；
- 数据许可、地域或保留权变为不明确；
- 发现 Research OS 可访问 AATS live secret、Runtime 写端或下单接口；
- 未分类缺口、时间语义或合约单位无法解释；
- raw 被覆盖、恢复不一致或证据 digest 不可复现；
- 预算预计超支 25% 或长期数据成本没有可接受方案；
- 为赶日期而降低 gate、扩大搜索或把 proxy 标记为真值。

## 6. 下一次正式决策

下一次正式决策点是 `G0 Gate Review`，不是 UI 展示或服务发布。会议输入为 `ROS-G0-009 G0 Closure Packet`；输出只能是：

- `PASSED`：C01--C06 全部关闭，解锁 G1；或
- `NO-GO`：保留未关闭条件、Owner、整改证据和下一次复审入口。

在 G0 Gate Review 前，本计划只授权 G0 收口和可逆 bootstrap。
