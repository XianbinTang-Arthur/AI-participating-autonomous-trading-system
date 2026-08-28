# RDP 衍生品 Phase 2 晋级证据生产闭环 P1 任务书

> 文档状态：现行实施任务书；LF-A 实施候选已完成聚焦验证，LF-B 衍生品 producer 尚未完成
> 最后核对：2026-08-28（起始基线 `main@c15ccd2d5057`，以本文档所在 HEAD 为准）
> 核对范围：完整 RDP 编排、Step 2/3 产物、`backtest-run/v2`、Phase 2 evidence consumer 与晋级选择器
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 决策摘要

当前“完整 RDP”能执行 legacy calibration/scan、Step 3、归因、执行现实性、治理与决策，但不能在
干净环境生成 Phase 2 可晋级证据。Phase 2 consumer 只接受 manifest-last、逐文件 SHA-256 绑定的
`backtest-run/v2` 及 `phase2-promotion-metrics/v1`；仓库没有正式代码生产后一文件，现有
`backtest-run/v2` 又明确只支持 SPOT。当前 RDP 楔子是 `BTC-USDT-SWAP`，不能借用
`BTC-USDT` SPOT 回测冒充永续合约证据。

因此本任务分为两个不可颠倒的切片：

1. LF-A：立即关闭跨 instrument scope 污染，并让完整管线/UI 准确暴露
   `derivatives_phase2_promotion_evidence_unavailable`，不得把“阶段进程成功”写成“可晋级研究完成”；
2. LF-B：在 Step 3 选出精确候选后，运行独立的衍生品资格轮，生成因果回测与 promotion metrics，
   经不可变资格轮 snapshot 锚定后再解锁自动晋级评估。legacy Step 2 不承担资本晋级证据生产职责。

## 2. 已复现根因

1. `rdp_run_full_pipeline.py` 的 Phase 2 只调用 `rdp_run_step2_research.py`；后者聚合 legacy
   `run_replay` calibration/scan，不生成 `backtest-run/v2`。
2. 公共 `aats backtest` CLI 只生成五个 v2 payload 与 `manifest.json`，manifest 的 artifact set
   不含 `phase2_promotion_metrics.json`。
3. Phase 2 consumer 要求该 metrics 文件存在、schema 为 `phase2-promotion-metrics/v1`、被 manifest
   hash 与 artifact-set fingerprint 同时绑定；缺失时正确返回 audit-only。
4. 公共 backtest harness 在任何 I/O 前拒绝 derivatives/MARGIN；它没有永续合约 funding、mark/index、
   initial margin、maintenance margin、liquidation、linear/inverse 合同与结算币种闭环。
5. 现有 Phase 2 metrics scope 只核 family/timeframe，没有把研究 symbol/instrument contract 与目标参数
   symbol 绑定。若直接补写测试形状文件，SPOT 证据可能污染 SWAP 评分，这是金融正确性错误。
6. 当前没有 `phase2_qualification_rounds/<round_id>` 这一独立发布边界，也没有 managed immutable
   qualification snapshot 将外部 v2 manifest 的 digest/size 固化为 canonical identity；后续重读文件即使
   逐文件自校验，也不能证明它仍是资格轮发布时那一份。把该锚点塞回 legacy Step 2 会混淆“参数探索”
   与“资本晋级资格”两个生命周期，并且 Step 2 尚未知道 Step 3 最终选中的精确参数身份。
7. Phase 2 consumer 曾在选中 latest canonical Step 2 后，仍把 `artifact_index` 或历史目录中的普通
   experiment 混入晋级聚合；旧的 eligible SPOT/实验结果因此可能补足当前 round 缺失的 combo。LF-A
   必须让 canonical round 成为排他的资格作用域：索引与历史目录只作审计展示，不能进入当前资格统计；
   LF-B 完成后则只允许显式指定的 immutable qualification round 进入 Phase 6。

## 3. 目标状态

一份可晋级的 Phase 2 combo 证据必须同时证明：

- 精确 `family + symbol + timeframe + dataset_version + window`；
- 版本化 instrument master/contract，明确 `SWAP`、linear/inverse、contract value/multiplier、base/quote/
  settle currency、tick/lot/min size；
- bar close 后决策、下一可交易事件成交的因果时间；
- funding 按真实时间语义入账，mark/index/reference 来源可追溯；
- fee、slippage、partial/no-fill、position、PnL、initial/maintenance margin 与 liquidation 逐事件可重放；
- 参数值及 Step 3 candidate lineage 的确定性指纹；
- promotion metrics 可从已绑定 decision/execution 明细重算，而不是孤立自报；
- manifest-last，不可变目录，所有 payload digest/size 与 artifact-set fingerprint 完整；
- Step 3 之后创建独立 `phase2_qualification_rounds/<round_id>`；其 manifest、`round_result.json` 与 managed
  DB snapshot 固化精确 Step 2/3 round、候选内容指纹、instrument snapshot identity、每个 v2 manifest 的
  路径/digest/size 与聚合 fingerprint；
- Phase 6 必须显式接收并消费该精确 qualification round，与目标 parameter set 的 symbol/combo/content
  完全一致；禁止通过 latest、artifact index 或 legacy Step 2 路径推断资格证据。

## 4. 实施分解

### 4.1 LF-A：失败关闭与真实性

- 为 Phase 2 evidence validator 增加 mandatory symbol/instrument scope；缺 symbol、SPOT/SWAP 不一致、
  contract fingerprint 不一致均 audit-only。
- 新 Step 2 聚合行显式写 `symbol`；旧行不猜测、不回填。
- 完整管线在进入 Decision 前生成 readiness preflight：若本轮四个 combo 没有精确、managed、immutable
  Phase 2 promotion evidence，Decision 可生成审计 round，但 research outcome 必须是 blocked/hold，UI
  显示唯一原因与下一步，不显示“完整研究已就绪”。
- 文档明确 legacy calibration/scan 的作用是参数探索和审计，不是资本晋级证据。
- 一旦选中 canonical Step 2（即使其有效诊断为零），历史 `artifact_index`/目录实验不得补充当前晋级
  统计；这些记录仅可作为审计展示。不存在 canonical round 时的 legacy fallback 也只能保持 audit-only。
- Decision 必须显式选择目标 Step 3 的父 Step 2 round，并记录其 managed snapshot typed fingerprint；
  promotion 校验需与 Step 3 snapshot 固化的父 ID/fingerprint 重验，禁止运行中更新的 latest Step 2
  替换当前候选的父证据。
- 资本晋级 hard gate 统一按目标 combo 独立判断：至少一个产生开仓的实验、最大开仓数至少 1、平均
  positive-edge ratio 至少 0.20。其他 combo 的高 edge 不能替目标 combo 补分；candidate selector、
  readiness、family/timeframe decision 与锁内 apply qualification 必须共享同一版本化阈值并独立重验。

### 4.2 LF-B1：衍生品回测领域合同

- 新增独立 `derivatives-backtest-run/v1`，不放宽现有 SPOT `backtest-run/v2`。
- 首个 instrument 仅 `BTC-USDT-SWAP` / `ETH-USDT-SWAP` linear USDT perpetual；inverse、dated futures、
  options 明确失败关闭。
- 输入必须绑定 source-aware Gold bars、funding、mark/index 与 instrument master version；缺任一必需流时
  失败，不用 close 价格伪造 mark/index/funding。
- 明确定义 initial capital、reporting currency、leverage、initial/maintenance margin、bankruptcy/liquidation
  价格、funding settlement 与 fee asset；Decimal 计算，禁止 float 资本记账。

### 4.3 LF-B2：可重算 promotion metrics

- 生产逐 bar decision facts，至少包含 action、selectable、execution compatible、expected edge 分解与
  参数指纹；metrics 从该已绑定明细确定性聚合。
- `phase2-promotion-metrics/v2` 至少含 symbol、instrument contract fingerprint、family、timeframe、
  total bars、opening count、positive-edge ratio、mean edge、selectable/execution-compatible ratio、window、
  dataset/source fingerprints。
- consumer 从 decision facts 重算计数与比率并与 metrics 比较；NaN/Infinity、重复 JSON key、非法 UTC、
  明细/汇总漂移全部失败关闭。

### 4.4 LF-B3：独立资格轮、完整 RDP 编排与不可变锚点

- 编排顺序固定为：legacy Step 2 参数探索 → Step 3 精确候选 → Phase 2 qualification round → Phase 3/4 →
  Decision。资格轮目录固定为 `artifacts/research/phase2_qualification_rounds/<round_id>`。
- 资格轮为四个精确 combo 运行 derivatives producer；每个子进程使用唯一 result sidecar/marker，禁止扫描
  global latest。父 manifest 必须绑定 exact Step 2 round ID、exact Step 3 round ID、候选文件 digest/size、
  parameter values fingerprint、symbol、dataset/window 与四组合拓扑。
- 每个 combo entry 必须保存 v2 manifest 的 path/SHA-256/size、artifact-set fingerprint、parameter values
  fingerprint，以及 instrument snapshot ID/digest/effective window。instrument contract 仍为
  `calculation_contract_only_unverified` 或来源/生效区间不确定时，资格轮只能 blocked/hold。
- managed immutable qualification snapshot 必须把上述父子身份整体固化；同 round ID 只允许 exact retry，
  任一字段漂移均 conflict。仅靠 bundle 内部自洽哈希不能替代该外部不可变锚点。
- Phase 3/4 与 Decision 消费同一 Step 2/3/qualification chain；Decision 必须显式接收
  `expected_phase2_qualification_round_id`。任一 combo 缺失、重复、partial/failed 或 lineage 不一致时只能 hold。
- UI 分开显示“任务执行状态”“研究完整性”“晋级资格”，避免绿色执行状态掩盖证据缺口。

## 5. 验收矩阵

1. SPOT `BTC-USDT` v2 bundle 不能支持 `BTC-USDT-SWAP` 参数晋级。
2. legacy Step 2 产物、缺 symbol 产物和测试手工 metrics 均保持 audit-only。
3. 四个 combo 各有一份 exact、非重复、manifest-last derivatives bundle；scope、参数、窗口、dataset 与
   exact Step 3 candidate 及 qualification parent manifest 精确一致。
4. 修改任一 decision/fill/funding/mark/metrics 文件、子 manifest digest、qualification DB snapshot identity
   或 result ref，
   consumer 均给稳定失败原因且零 recommendation/apply 写入。
5. metrics 的六项核心统计全部由 hash-bound 明细重算；计数、有限性与 `[0,1]` 比率边界通过负向测试。
6. linear perpetual quantity/notional/fee/PnL/margin/liquidation/funding 使用 golden vectors 与真实
   PostgreSQL 集成验证；断流、乱序、重复 funding、cadence gap 和空窗口失败关闭。
7. 两条并发完整管线不交叉消费 bundle；中断恢复只按 exact result ref，不能回退 latest。
8. Windows lint/unit、WSL2 PostgreSQL integration、determinism/recovery/contract 测试和独立复审无未关闭
   P0/P1；模拟环境端到端只产生审计/recommendation，不自动 apply。

## 6. 非目标与门禁

- 本任务不允许用 SPOT 结果乘系数近似 SWAP，不伪造历史 funding/mark/index/L2。
- 不因实现 producer 自动放宽 G0/G1、live profile、真实订单、真实资金或 recommendation apply 门禁。
- LF-B 若缺真实可授权数据，只能完成合成 golden-vector 与失败关闭验证；现场数据充分性保持 UNKNOWN。
- 在 LF-B 验收前，“完整 RDP”仅表示编排执行完整，不表示可产生收益或可上线。

## 7. 当前下一步

1. **LF-A 实施候选已完成**：symbol/scope 绑定、稳定
   `derivatives_phase2_promotion_evidence_unavailable`、Step 2 显式 symbol 与 readiness 审计投影均有
   聚焦回归；仍须随本变更执行全量测试与独立复审。
2. 形成 `derivatives-backtest-run/v1` ADR 与 JSON schema/golden vectors。
3. 实现单一 `BTC-USDT-SWAP independent_15m` 合成纵向切片，再扩展首轮精确四 combo；ETH 仅在 BTC
   资格轮契约稳定后扩展。
4. 新建独立 qualification round manifest/result/snapshot，并把 exact Step 2/3、instrument snapshot 与
   四个子 manifest 锚定；完成并发、恢复、确定性与 PostgreSQL 验收后再接入 Phase 6。
