# AATS Research OS 来源准入与收益优先级纠偏

> 文档状态：现行纠偏决议
> 决策日期：2026-08-29
> AATS 核对起始基线：`main@3c8d3c42`
> Research OS 核对起始基线：`main@22a465b`
> Research OS 落地基线：`main@b67a59f2c1c69f313796410a492c8a87b03f3235`
> 适用范围：数据源选择、G0/C03 许可范围、G1 工作包和 RDP 自主交付优先级
> 安全边界：不授权 live、真实资金、下单、凭证读取、付费采购或绕过地域/KYC 限制

## 1. 决议

1. **Binance 明确排除。** 它不是交易场所、研究数据源、验证源、fallback、许可对象、探针或上线
   前提。AATS 运行代码本来没有 Binance 集成；本次同时移除独立 Research OS 合同与计划中的硬绑定。
2. **当前来源基线为 OKX 主源、第二来源 `UNBOUND`。** 第二来源未选定不会阻断 OKX 单源假设研发，
   但任何跨场所稳健性或来源独立性结论都必须保持 `UNKNOWN`。
3. **需要认证或商业关系的数据源先调研、后引用。** 任何需要账户、KYC、API Key、付费订阅、
   私有授权或地域资格的候选，在进入架构前必须完成官方访问条件、地域限制、许可用途、字段、历史、
   频率、费用、保留/派生权和退出方案审查；凭证只能在操作者明确授权后配置。
4. **公开来源可以自主选择，但“无需认证”不等于“可用于 AI 研究”。** 每个公开候选仍需核对官方
   文档、许可/条款、robots、限频、数据连续性、时间语义和收益假设价值。通过后可自主建立最小技术
   试验；许可不清或用途冲突时必须拒绝。
5. **工程优先级服从可证伪的净收益证据。** 除非直接阻断不可回填数据、研究正确性、真实成本或
   资金安全，通用防御、完整 UI、平台抽象和外围治理不得挤占 Alpha 研究主线。

## 2. 当前来源决定

| 来源 | 官方证据（2026-08-29 查阅） | 当前决定 | 理由与边界 |
| --- | --- | --- | --- |
| OKX public market data | [API Agreement](https://www.okx.com/help/okx-api-agreement)、[API 文档](https://www.okx.com/docs-v5/en/) | `PRIMARY / SELECTED_PENDING_LICENSE` | AATS 现有资产以其为主；适用主体、地域、保存、派生和 AI 用途仍需 DLR 真人签字 |
| Binance | 操作者明确不可使用 | `EXCLUDED_BY_OPERATOR_CONSTRAINT` | 不进入代码、许可门、计划或备用路径；不规避账户/地域条件 |
| Coinbase Exchange market data | [Market Data Terms](https://www.coinbase.com/legal/market_data)（last updated 2026-08-07） | `EXCLUDED_BY_TERMS_FOR_AI_ML_USE` | 第 3.5 项禁止使用 Market Data 开发、训练、验证、benchmark 或运行 AI/ML；本项目预期用途直接需要 Market Data |
| Kraken public market data | [API 文档](https://docs.kraken.com/api/)、[按地域适用的 Terms](https://www.kraken.com/legal) | `UNSELECTED / LICENSE_AMBIGUOUS` | 公开端点存在，但尚不能确认本项目所需的保存、派生研究和 AI 使用权；不接入 |
| Federal Reserve H.15 | [官方 Data Download Program](https://www.federalreserve.gov/datadownload/Choose.aspx?rel=H15) | `SELECTED_FOR_OFFLINE_CONTRACT_SPIKE_PENDING_SOURCE_RECORD` | 可用于低频利率 regime；尚未形成仓内 source record/许可结论，且 2026 年 11 月起分发路径将变化 |
| BLS Public Data API v1 | [API FAQ](https://www.bls.gov/developers/api_faqs.htm)、[Terms](https://www.bls.gov/developers/termsOfService.htm) | `SELECTED_FOR_OFFLINE_CONTRACT_SPIKE_PENDING_SOURCE_RECORD` | v1 无需注册但配额低且不返回完整 metadata；历史 PIT vintage 不可由当前 observation 回填 |
| Cboe VIX 历史数据 | [官方历史页面](https://www.cboe.com/tradable_products/vix/vix_historical_data) | `CANDIDATE / LICENSE_REVIEW_REQUIRED` | 有 regime 价值，但公开下载页面尚不足以确认自动保存和派生权；审查前不实现 |

`SELECTED_FOR_OFFLINE_CONTRACT_SPIKE_PENDING_SOURCE_RECORD` 不是“许可批准”：只记录其技术与研究价值
值得进入下一次 source record 审查。在官方来源记录、可用时间/修订语义、限频、保存/派生用途和退出
方案完成前，不写持续 collector，也不把它加入 G0 active license scope。

BLS v1 observation 只能标记为 `CURRENT_AS_RETRIEVED`，并同时记录
`revision_status=UNKNOWN`、`historical_vintage=UNKNOWN` 和 `fetched_at`；只有存在官方发布时间证据
时才填 `available_at`。结合官方 release calendar/archive，并从启用日开始逐次快照后，才能建立
前瞻 vintage；禁止把今天取得的值回填为历史时点可得值。H.15 同样必须保存获取、可得和
revision/correction 事实，并使用可替换的版本化分发适配器。

## 3. 收益优先级与资源预算

未来自主开发按下面的工程时间预算执行：

- 60%：已有数据变现与可证伪 Alpha——强平、成交、L2、OI、funding、long/short、volume profile，
  以及 BTC/ETH 对称研究；
- 25%：真实成本、样本外和 paper forward——手续费、funding、滑点、延迟、fill ratio 与容量；
- 15%：直接保护研究正确性和资金边界的最小治理、可复现与恢复；
- 通用防御和 UI 不单列预算。它们只有在直接阻断上面三项时才可进入本轮最高优先级。

每个研究假设必须在运行前写明机制、特征、持有期、成本口径、适用 regime、样本外切分和
`GO / ITERATE / KILL` 阈值。失败结论是正常研究产出；禁止通过放宽统计门、忽略成本或重复窥视
holdout 制造“正收益”。

## 4. 对 G0/G1 的影响

- G0 C03 active 许可矩阵只包含当前已选范围；排除/未选择来源不得作为必过条目，也不得用 `DENY`
  项永久卡住 Gate。机器校验必须保证 source selection、active license、capability provider 和
  instrument scope 精确一致；Closure Packet 同时需要 `SOURCE_SELECTION` 与 `LICENSE_MATRIX` 证据。
- 独立 Research OS 已在 `b67a59f2c1c69f313796410a492c8a87b03f3235` 将 source contract 升级为
  OKX-only v2，第二来源为 `UNBOUND`；该提交只证明本地静态合同与测试，不证明远端发布、许可通过
  或持续采集。
- 原 G1 的 Binance 168 小时探针删除；第一批 30 日工作改为 OKX 连续性、不可变 Raw、确定性回放，
  并并行交付首批强平/盘口/订单流/OI-funding 假设的真实研究输入。
- G0 未经真人任命、许可、预算、IAM 和签字通过时，Research OS 持续采集仍为 `G1_LOCKED`；这不妨碍
  在旧 AATS 获准范围内恢复现有公开采集和完成离线研究。

## 5. 验收

- AATS 活跃运行代码、配置和部署不存在 Binance 依赖；负面测试不再用 Binance 冒充通用非法场所。
- Research OS 的领域枚举、能力矩阵、字段绑定、许可矩阵和 G1 计划不存在 Binance 活跃能力或前提。
- 保留凭证泄漏扫描中的 Binance 名称，因为它是拒绝秘密进入仓库的防线，不代表支持该来源。
- 历史任务书不改写当时事实，但所有可能误导执行的文件顶部均指向本纠偏决议。
- 自动化任务每轮必须更新盈利证据漏斗：新增证据、保留/修订/淘汰假设、当前最佳成本后 OOS/paper
  结果、下一决策门缺口与下一项最高价值动作；不得用代码量、测试数、表数量或页面数量代替进展。

## 6. 当前未知与真人事项

- OKX 的最终地域/许可签字仍未完成，不得由 AI 或代码测试代签。
- 第二独立来源尚未选定；在有合规且用途相容的候选前，跨来源结论保持不可得。
- H.15/BLS 仍只是待 source record 的离线合同候选；适配器、发布时间/vintage 合同和研究消费者
  尚未实现，任何历史 PIT 不可得部分必须保持 `UNKNOWN`。
- 本决议不证明任何 Alpha、paper PnL 或实盘盈利。

## 7. 落地与验证

- Research OS 本地提交：`b67a59f2c1c69f313796410a492c8a87b03f3235`；工作树提交后干净；
- source selection、active license、capability/instrument scope 的机器一致性检查通过；
- 第二来源在 `ALLOW` 前只能保持 `UNBOUND`；Binance/Coinbase provider-family 别名重引入负测通过；
- Research OS contracts/governance/unit/golden 全量测试与 Ruff 通过；G0 readiness 为
  `machine_contract_state=PASS / formal_gate_state=G0_OPEN / g1_unlocked=false`；
- AATS 全量 unit 在本轮变更前后相关代码口径下通过；本轮不部署、不采集、不访问私有账户、不下单。
