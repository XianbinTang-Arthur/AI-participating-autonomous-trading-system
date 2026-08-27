# RDP 衍生品合约数量、名义价值与损益统一 P0 任务书

> 文档状态：现行实施任务书；P0-A 代码切片已完成单元验证与独立复审，WSL2 集成/运行验证未执行；其余阶段未验收
> 最后核对：2026-08-27（起始代码基线 `main@7945a078a0447492af8cfbee81aaf138ea8a665b`）
> Freeze 分类：`LF-A`（金融正确性与数据真实性修复，不扩展 Legacy 研究能力）
> 核对范围：OKX instrument 解析、数量转换、RDP 微观结构 Silver、OHLCV replay、
> L2/execution realism、campaign/bundle/artifact fingerprint 与相关测试/操作说明
> 运行状态边界：当前公开 instrument 响应只证明核对时的产品定义，不证明历史有效期；
> 本任务书不证明当前账户、仓位、订单、容器或交易就绪状态
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本任务关闭 Legacy Freeze 登记中的 P0：所有衍生品路径必须以一个明确、版本化、可校验的
instrument contract 解释交易所张数、内部数量、base exposure、quote notional、手续费、PnL
和最小下单单位。任何路径不得继续把 OKX 原生合约张数直接当作 base 数量，也不得依靠
`0.01`、`price * size` 或缺失元数据时的 identity/zero fallback 生成看似有效的货币值。

本任务不新增交易所、数据产品、策略、因子或研究 UI，不启动 Research OS G1，不触发真实
订单或参数应用。Research OS 的 Instrument Master 仍是新平台长期真源；本任务只对 AATS
Legacy 中已经存在且继续影响安全/研究真实性的路径做最小完整修复。

## 2. 整改前行为与根因

静态审查确认以下互相放大的错误：

1. `microstructure_silver_merger.py` 的 trade、whale、volume profile 和 liquidation 聚合均以
   `price * exchange_size` 计算金额。OKX derivatives 的 `sz` 是合约张数；例如线性
   BTC-USDT-SWAP 的 contract value 为 `0.01 BTC` 时，结果会被放大 100 倍。
2. replay 的 PositionTracker 默认 `contract_multiplier=0.01`，但 FillSimulator 的 fee
   notional 使用 `contracts * price`，造成 PnL 与费用采用不同单位。该默认值也只适用于某个
   时点的 BTC 线性合约，不能解释 ETH、反向合约或产品规格变更。
3. L2/execution-realism 仍存在 `contracts * price` 和 BTC `0.01` 默认；缺失 bar/notional
   的部分路径把未知写成 `0`，使“无证据”和“真实零”不可区分。
4. `quantity_rules.py` 和 `okx_adapter.py` 重复实现 `contracts * ctVal`。它们未使用
   `ctMult`/`ctType`，且对 inverse contract 不成立；元数据缺失或无效时退化为 identity，
   将未知单位静默包装成内部 base quantity。
5. `InstrumentMetadata` 尚未保存 OKX 已提供的 `ctType` 和 `ctMult`；RDP source、campaign、
   bundle 和研究 artifact 也未统一绑定 instrument snapshot digest/effective window。

根因不是单个公式写错，而是系统没有一个跨执行与研究共用的金融单位合同；单位信息没有
进入 lineage/fingerprint，测试又以硬编码样例固化了错误结果。

## 3. 模块职责与领域模型

建立单一纯 Decimal 合约算术真源，领域对象至少包含：

| 字段 | 语义 |
| --- | --- |
| venue / symbol / instrument type | 来源与产品身份 |
| contract type | `linear` / `inverse`；未知或矛盾必须失败关闭 |
| base / quote / settle currency | 数量、报价和结算币种 |
| contract value / multiplier / value currency | 一张合约面值及其币种 |
| lot / min size / tick | 交易所张数和价格离散化约束 |
| observed/effective window | 元数据何时被观测、何时可用于历史解释 |
| source schema / digest | 来源版本与确定性指纹 |

共享算术层只做确定性转换和校验；采集、Silver、replay、execution realism 与执行适配层只能
调用它，不得复制公式。API/序列化层保留来源单位和规范化单位，不能只留下一个无单位的
`quantity`。

## 4. 输入/输出接口与金融公式

以 `contracts` 为交易所张数，`face = ctVal * ctMult`，`P` 为正价格：

- linear 且 face currency 为 base：
  - `base_quantity = contracts * face`
  - `quote_notional = abs(contracts) * face * P`
  - quote-settled long PnL = `contracts * face * (exit - entry)`；short 方向取反
- inverse 且 face currency 为 quote/USD：
  - `base_equivalent_at_P = contracts * face / P`
  - `quote_notional = abs(contracts) * face`
  - base-settled long PnL = `contracts * face * (1 / entry - 1 / exit)`；short 方向取反
- spot：内部 quantity 即 base quantity，quote notional 为 `abs(quantity) * P`。

负数量只表达方向；金额、手续费基数和风险 notional 默认取绝对值并单独保存 side。零、负价、
非有限 Decimal、非正 face/lot/min、未知 contract type、币种矛盾或 derivative metadata 缺失
必须给出稳定 reason code 并失败关闭。

官方核对依据：OKX `GET /api/v5/public/instruments` 对 derivatives 的 `lotSz/minSz` 定义为
合约张数，并提供 `ctVal`、`ctMult`、`ctValCcy` 和 `ctType`；OKX 的 linear/inverse PnL 公式
分别使用价格差和倒数价格差。来源：

- <https://www.okx.com/docs-v5/en/#public-data-rest-api-get-instruments>
- <https://www.okx.com/en-gb/help/futures-pnl-calculation-rules>

2026-08-27 的公开响应只作为实现黄金向量的核对输入，不允许据此把同一规格追溯套用到早于
`observed_at` 的历史数据；历史有效期必须有独立来源证据。

## 5. 数据库 schema、表、索引与约束

分阶段实施，禁止在没有 migration/rollback 的情况下直接改库：

1. P0-A 不改 schema：扩充进程内 `InstrumentMetadata` 与纯算术真源。
2. P0-B 评估并实施最窄版本化 instrument snapshot。优先复用现有 source registry/campaign
   manifest JSON，但若无法表达唯一 digest、有效窗口和不可变引用，则新增专用表及约束。
3. source、campaign、bundle、rebuild、Gold/replay artifact 必须保存同一 snapshot digest；
   digest 缺失或窗口不覆盖数据时不得取得 contract-aware eligibility。
4. 旧 Silver 金额列不原地伪修；通过新 dataset version 确定性重建。旧版本保留 lineage 并
   标记 `contract_metadata_unbound`，不再参与收益结论或 promotion。

任何 migration 必须同时提供空库升级、已有库升级、约束负向测试和 rollback 演练。

## 6. 事务、一致性与并发

- instrument snapshot 按 digest 不可变写入；同一来源 identity + effective window 的并发写入
  必须收敛到相同 payload，否则冲突失败。
- campaign/bundle/artifact 在创建时绑定 digest，运行中不得被“最新 metadata”漂移替换。
- 重建使用新 dataset version 原子写入，失败保留旧数据且不切换 eligibility。
- execution 路径只使用同一 account snapshot 中的 instrument metadata；数量规划到提交期间的
  规格变化必须触发重新规划或拒绝，不允许用旧 lot/face 提交。

## 7. 授权、认证与数据安全

只允许读取 OKX 公开 instrument endpoint 或已经获准的现有 account read snapshot；不得读取、
输出或固化凭证。公开响应只持久化金融合同白名单字段和来源摘要，不保存 HTTP header、账号或
网络标识。任何 live 下单/资金副作用仍由现有门禁阻断。

## 8. 错误处理、失败关闭与幂等

稳定错误至少包括：

- `derivative_instrument_metadata_required`
- `contract_type_unknown_or_inconsistent`
- `contract_face_value_invalid`
- `contract_value_currency_inconsistent`
- `inverse_quantity_requires_reference_price`
- `instrument_snapshot_window_unproven`
- `instrument_snapshot_digest_mismatch`

研究聚合缺少有效 metadata 时，绝对 notional/fee/PnL 输出必须为 `NULL/unknown` 并附 reason，
不得写 `0` 或旧公式结果；仍可证明与 contract multiplier 无关的计数/比例必须明确标注其单位
边界。相同输入、snapshot digest 和算法版本必须得到相同 Decimal 结果与 fingerprint。

## 9. 状态转换与生命周期

```text
exchange/source event
  -> resolve immutable instrument snapshot for event time
  -> validate product type, currencies, face, lot/min/tick
  -> normalize contracts/base/quote/settlement values
  -> persist source units + normalized values + instrument digest
  -> aggregate/replay/rebuild with the same digest
  -> eligibility and promotion gates
```

任一步无法证明 instrument contract 时进入 `CONTRACT_METADATA_UNKNOWN`，不得继续成为可发布
的研究证据。后补 metadata 只能触发新版本重建，不能静默改写旧 artifact。

## 10. 缓存与性能

运行内可按 `(venue, symbol, snapshot_digest)` 缓存已验证的纯对象；缓存不得按 symbol 单独覆盖
历史版本。每个 bar/批次解析一次 metadata，行级计算复用已验证对象。新增 digest 计算只覆盖
小型规范化 JSON，不扫描 raw tick 表。

## 11. 日志、监控与审计

至少监控：metadata resolve 成功/失败数、unknown contract type、窗口不覆盖、digest mismatch、
旧 artifact 失格数和待重建数。日志仅输出 symbol、reason code、digest 前缀和数据窗口；不得
输出凭证或完整私有 payload。UI 必须把未知和真实零分开。

## 12. 分阶段交付与测试策略

| 阶段 | 交付 | 关键测试 |
| --- | --- | --- |
| P0-A | `InstrumentMetadata` 补齐 ctType/ctMult；单一纯 Decimal contract arithmetic；quantity rules 复用并对 inverse/矛盾输入失败关闭 | spot、BTC/ETH linear、BTC inverse 黄金向量；round-trip；非有限/缺失/矛盾负向 |
| P0-B | 不可变 instrument snapshot、effective window、digest 与 source/campaign/bundle/artifact 绑定 | migration/rollback；并发收敛；digest 改变引起 fingerprint 改变；历史窗口负向 |
| P0-C | trade/whale/volume profile/liquidation 使用绑定 metadata；缺失时金额 unknown | linear/inverse/缺失 metadata；BTC 100 倍错误回归；新 dataset version 重建 |
| P0-D | replay fill、fee、PnL、equity 统一 contract spec；移除默认 `0.01` | 三种订单类型；linear/inverse PnL/fee/equity；CLI/manifest 显式 metadata |
| P0-E | L2 replay、market alignment、simulation calibration 逐路径单位审计和迁移 | fee/notional 黄金向量；unknown != zero；base-vs-contract 输入边界 |
| P0-F | 旧 artifact 资格迁移、受影响证据清单、确定性重建和独立复核 | 旧版本拒绝 promotion；重建 fingerprint；Postgres 集成；复核无 P0/P1 |

每阶段先跑 focused tests，再跑相关 integration、Ruff 和全量 unit；需要数据库的集成验证只在
规定 WSL2 环境执行。测试绿色不能替代历史 metadata 有效期或现场运行证据。

## 13. 迁移、回滚与兼容

- P0-A 先增加字段与显式算术 API，旧调用保持可加载；derivative 的未知/矛盾单位不再允许
  静默 identity fallback。
- 研究列语义变化必须提升 dataset/algorithm/schema version，禁止用同一版本覆盖旧结果。
- 旧 artifact 保留可追溯路径但失去 contract-aware eligibility；只有绑定有效 snapshot 的
  新重建版本可恢复资格。
- 代码回滚不得重新启用错误金额；若新路径异常，应保持 `unknown/blocked` 并停用受影响研究，
  而不是回退到 `price * contracts`。

## 14. 配置与环境隔离

禁止为单个 symbol 新增无消费者的 managed-profile 假开关。contract 定义来自可审计 snapshot，
不从 `.env.*`、隐式默认或“当前最新”全局变量取得。simulation 和 live profile 可以引用不同
snapshot 来源，但必须共享同一 schema/算术和失败关闭规则。

## 15. 代码组织与依赖

预计主要修改：

- `aats/schemas/exchange.py`
- 新的共享金融合同/算术模块（不得依赖 DB、网络或 service runtime）
- `aats/services/execution_engine/quantity_rules.py`
- `aats/services/execution_engine/okx_account.py` / `okx_adapter.py`
- `aats/data_platform/merge/microstructure_silver_merger.py`
- `aats/data_platform/replay/backtest/`
- `aats/data_platform/execution_realism/`
- instrument lineage/fingerprint、migration、操作手册与测试

不新增第三方依赖。数据平台不得依赖 live account service 才能重建历史；它只消费不可变、
已验证的 instrument contract DTO/snapshot。

## 16. 验收标准、当前进度与退出条件

P0 只有同时满足以下条件才可关闭：

1. 全仓不再有衍生品 `price * exchange_contracts`、隐式 `0.01` 或 metadata 缺失 identity/zero
   fallback 生成货币真值；允许的固定数值仅存在于明确命名的测试输入。
2. linear/inverse/spot 黄金向量覆盖 quantity、notional、fee、PnL、lot/min quantization；所有
   Decimal、币种和方向语义可追溯。
3. RDP source→campaign→bundle→Gold/replay artifact 绑定有效 instrument digest，fingerprint
   对 metadata 变化敏感；历史窗口无证据时失败关闭。
4. 受影响旧 artifact 已列清单、失格且不会被 UI/promotion 当成有效收益证据；可重建部分以新
   dataset version 重建，不可证明部分保持 unknown。
5. focused、相关 unit/integration、migration/rollback、确定性恢复、Ruff 和全量 unit 通过；
   独立复审无未关闭 P0/P1。
6. 模拟环境端到端验证明确使用哪个 instrument digest；无 live 订单、资金、参数应用或凭证
   副作用。

当前进度（2026-08-27）：P0-A 代码切片已完成。`InstrumentMetadata` 现在显式保存
`ctMult/ctType` 且不再为 derivative 缺失面值、乘数或 lot/tick/min 规则猜测默认值；统一纯
Decimal 领域合同覆盖 spot/linear/inverse 黄金算术、币种与 symbol/type 一致性，并使出站数量、
价格量化和序列化不受调用方 Decimal context 影响。execution quantity 仅开放 spot/linear，
inverse 和缺失/矛盾 metadata 均失败关闭；阻断审计 payload 不含伪造 `sz`。账户刷新还会验证
所有 tracked derivative contract，flat account 也不能在元数据缺失时报告 ready。

验证证据：相关聚焦测试 `204 passed, 20 subtests passed`（其后新增复审回归后的四文件集为
`142 passed, 20 subtests passed`）；最终 Windows 全量 unit 为 `4878 passed, 30 skipped,
94 subtests passed`，Ruff 通过。独立第三/四轮复审最终结论为 P0-A 代码切片 `ACCEPT`，无未关闭
P0/P1。五组受严格合同影响的 WSL2 integration 夹具已补齐显式合同字段，但本轮未在 WSL2 执行
集成测试，也未部署或触发任何 exchange/live 副作用；运行中的历史恢复 campaign 未被干预。

P0-B 至 P0-F 均未完成，因此上述结论不是整个 P0 验收。当前 RDP 的衍生品绝对 notional、
fee/PnL 和由其派生的收益证据仍不得声明 contract-aware 或可用于实盘。
