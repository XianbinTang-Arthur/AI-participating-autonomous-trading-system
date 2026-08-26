# RDP 历史数据恢复与持续采集加固任务书

> 文档状态：实施任务书 / 工程实施完成，现场验收部分完成
> 编写日期：2026-08-26
> 起始代码基线：`51448768bb3ff08fa44066d286f7383800d8d744`
> 最后核对：2026-08-26（实现基线 `fe5596fd5ee4`；derivatives generation `fe5596fd5ee4-20260826T151737Z-392-3966`）
> 工作区边界：起始工作区包含一组尚未提交的 RDP live attribution lineage 修复；必须先独立复审、验证并收口，禁止由本任务覆盖或重复实现
> 核对范围：当前代码、迁移、RDP workflow、采集器、保留脚本、现行 RDP 文档与 OKX 官方公开历史数据能力
> 运行时边界：本文是任务清单，不证明当前数据库覆盖、采集器新鲜度、容器健康、交易所账户状态或收益能力；实施期间禁止启动 live profile、提交真实订单、应用参数建议或输出凭证

## 1. 业务目标与边界

### 1.1 目标

建立一条可以长期运行、可恢复、可审计的数据供应链：

1. 盘点当前 RDP 各层真实覆盖率，区分“无数据”“已删除”“尚未聚合”“采集器中断”和“不可归因”；
2. 从 OKX 官方来源回填可回填的 OHLCV、资金费率、逐笔成交、历史 L2 和标记价格 K 线；
3. 对历史 OI tick、公共强平事件、本地 BBO/books5 采样、mark tick 和采集器运行证据只做持续重新采集，不伪造历史；
4. 在删除热数据前完成不可变归档和校验，消除当前 7/14/30 天保留策略造成的不可逆数据损失；
5. 将 live capture eligibility 与 historical research eligibility 分离，禁止通过伪造 heartbeat、统一假批次或插值样本绕过门禁；
6. 从合格原始数据幂等重建 Silver、Gold、质量报告、artifact 索引和 Research Factory 输入；
7. 完成 lineage、归因、执行事实和 readiness 的失败关闭链路；
8. 在 UI、指标、告警和 runbook 中展示真实覆盖、来源、缺口、任务状态和停止原因；
9. 只在 derivatives 模拟环境执行运行验证，所有 live profile 保持副作用前失败关闭。

### 1.2 非目标

- 不承诺策略盈利或 production-ready；
- 不把第三方数据包装成 OKX 官方数据；
- 不从成交、价格跳变或模型估计反推出“真实强平事件”；
- 不为旧 intent 猜测或补写决策 lineage；
- 不将 mark-price candle 冒充实时 mark tick；
- 不将 OKX 历史 L2 冒充 AATS 当时实际收到的 1 Hz/2 Hz 本地采样；
- 不读取、打印、提交或记录 `.env.*`、DSN、API key、密码、session secret 或 token；
- 不手工执行 Docker Compose，不使用 rsync，不绕过标准部署入口；
- 不自动 approve、release、apply 或 rollback 运行参数。

## 2. 已确认的当前边界

1. 当前工作区已有 `rdp_live_attribution_lineage_fix_sow_2026_08_26.md` 对应的未提交实现，涵盖 intent lineage、精确归因、readiness 和 schema migration；这是本任务的前置依赖。
2. `rdp_deep_backfill_api.py` 已支持 OKX `history-candles` 的 15m/1H 回填。
3. `rdp_deep_backfill_funding.py` 已支持资金费率历史回填。
4. 当前没有完整的官方逐笔成交、历史 L2、历史 mark-price candle 导入链路。
5. 当前微观结构 live gate 对一个 UTC 对齐的 15 分钟窗口要求至少 720 条 BBO、720 条 books5、1 笔成交、1 个 OI 样本，并要求 funding、mark、两个 collector freshness、dataset version、ingest run 和质量标记一致。
6. 当前热库保留策略会删除 30 天前成交、14 天前 BBO/books5、7 天前 OI/funding/mark tick；现有清理任务没有“归档校验成功后才删除”的硬依赖。
7. 当前采集器实际使用公开 `trades` 频道，但模块顶部说明仍写 `trades-all`；强平采集器顶部关于历史 REST 保留期的说明也已与当前 OKX 官方能力不符。
8. 当前静态事实不能证明目标数据库中实际覆盖了哪些时间段；实施前必须重新只读测量。

### 2.1 2026-08-26 实施进度边界

以下状态只描述当前工作区与已经执行的验证，不把“代码已实现”写成“现场数据已恢复”：

| 范围 | 当前状态 | 仍需现场证明 |
| --- | --- | --- |
| 前置 lineage | 已独立复审、测试并提交为 `c1b015ec`；部署后完整 RDP 已使用新 intent 重新核验 | 四个策略/周期组合的精确 replay/live 对齐仍为 0，readiness 正确失败关闭 |
| WSL2 Python | 规定路径 `~/aats-venv` 已恢复；bootstrap 已按固定 Python 3.12.14、uv 0.12.5、发布资产 SHA-256 和 Linux hash lock 实际重跑；关键依赖导入成功 | 标准部署同步后继续使用该路径，不再把环境缺失列为业务阻断 |
| provenance/schema | contract、source/import/archive/gap/bundle/rebuild/continuity 模型与 Batch B stage 18 已实现；ORM 总数 98；目标模拟库已由标准部署完成迁移并通过 ledger/schema guard | source registry、实际导入和恢复记录仍随后续受控样本产生 |
| coverage/archive/retention | 只读覆盖 artifact、不可变 Parquet、resume、archive-before-delete 与恢复分类已实现；目标库 v5 快照已生成且无 `audit_failed` | 单日容量、随机恢复演练和真实到期分区删除 0/正确行数证据仍未执行 |
| 官方历史导入 | confirmed OHLCV、funding、trade REST/file、L2 file 与 mark bar proxy 已实现；raw SHA-256、schema、序列、gap、唯一行数与 bundle 失败关闭 | 1 日官方样本；30/90 日需逐级容量批准；L2 官方文件尚未由本任务提供 |
| 持续采集 | OI/mark/trades/BBO/books5/liquidation continuity、generation、drop/gap 与终态加固已实现；两个 collector 已部署、健康且持续 flush | 受控重连、DB outage、进程 kill/restart 和跨日连续观察仍未执行 |
| 双准入与重建 | live/historical eligibility 和兼容矩阵已分离；bundle-scoped orderbook/trade-flow Silver 重建已实现 | OI/funding/liquidation/Gold 的 source-aware 扩展仍属于后续研究输入建设，不得声称已重建 |
| API/UI/告警 | 数据治理读模型、Workspace 卡片、恢复矩阵与可靠性告警已实现；部署后的后端读模型和 bounded snapshot 路径已随完整 RDP 验证 | 服务重启使既有登录会话失效，签名页面视觉验收需操作员重新登录后补做 |
| 执行事实/campaign/L2 replay | 未伪造；旧 intent 保持不可归因 | 需要只读账户授权、合格 30/90 日数据和独立预注册研究；当前保持 `UNKNOWN`/NO-GO |

因此，本任务可以完成工程基础设施与一日受控样本验收，但没有官方 L2 文件、只读账户授权或足够连续观察时间时，RDP-DATA-025、052、053 及 30/90 日扩展不能被诚实标记为完成。它们不是用伪数据或放宽门禁可以解决的软件缺口。

### 2.2 2026-08-26 静态与隔离验证证据

- Windows Ruff：`aats/` 通过；新增脚本与测试的定向 Ruff 通过；
- Windows 完整单元回归：`4794 passed, 30 skipped, 94 subtests passed`；skip 均保留既有环境/可选依赖边界；
- 依赖供应链：runtime 47、CI 41、外部镜像 9 项 lock contract 通过；
- WSL2 bootstrap：`~/aats-venv` 为 Python 3.12.14，`WSL_VENV_READY`，且 `psycopg`、`pyarrow`、`pytest`、`SQLAlchemy` 可导入；
- WSL2 隔离 PostgreSQL：Stage 18 在 Testcontainers PostgreSQL 16 上的完整迁移、幂等、回滚、修复和约束验证共 `3 passed`；
- WSL2 目标模拟部署：`fe5596fd5ee4-20260826T151737Z-392-3966` 成功；七个核心应用/采集容器均为 `healthy`、重启计数为 0；
- 完整 RDP：`task_235c5e4eb2a7` / `run_ff3e022b420444f7` 的 10 个步骤均成功结束，最终以 `blocked_by_attribution` 正确 NO-GO，未应用任何参数；
- 上述证据仍不替代官方 1 日样本、归档恢复、故障注入、跨日采集和签名 UI 的后续现场验收。

## 3. 模块职责与领域模型

### 3.1 模块职责

| 模块 | 职责 | 明确不负责 |
| --- | --- | --- |
| Coverage Auditor | 只读统计表级、字段级、窗口级覆盖、缺口、重复和 lineage | 补数据、删数据、修正历史 |
| Source Registry | 登记来源、版本、获取时间、校验和、许可边界和时间语义 | 隐藏不同来源之间的差异 |
| Historical Importers | 可恢复下载、校验、落地官方历史数据 | 连接交易执行链路 |
| Live Collectors | 持续采集 OI、mark、trades、BBO、books5、liquidations 和运行证据 | 声称补回启动前的历史 |
| Archive Manager | 分区归档、校验、恢复演练和 archive-before-delete | 未校验即删除热数据 |
| Eligibility | 分别判断 live capture 与 historical research 数据资格 | 为获得 PASS 降低或伪造门槛 |
| Silver/Gold Builders | 从已登记的合格来源确定性派生研究数据 | 修改原始事实 |
| Attribution/Readiness | 精确绑定 intent、市场窗口和执行事实，未知时失败关闭 | 用时间宽窗猜测 lineage |
| Operator UI/API | 展示覆盖、来源、缺口、任务、错误、取消和重试 | 暴露 DSN、凭证或提供 live 绕过 |

### 3.2 核心领域对象

- `DataSourceRecord`：来源类型、供应方、接口/文件、schema 版本和使用边界；
- `HistoricalImportRun`：下载、校验、解析、落库、聚合的可恢复运行；
- `ArchivePartitionManifest`：symbol、dataset、UTC 日期、行数、时间范围、SHA-256 和 schema；
- `DataGapRecord`：缺口范围、原因、是否可恢复、发现和关闭证据；
- `DatasetBundle`：一次研究使用的多个来源及各自版本，不伪造公共 ingest run；
- `HistoricalResearchEligibilityReport`：覆盖、来源、校验和、时间因果和质量门结果；
- `LiveCaptureEligibilityReport`：继续使用采集器 freshness、样本数和单次 live ingest lineage；
- `DataRebuildRun`：Silver/Gold/artifact 重建范围、代码版本和输入 bundle；
- `CollectorContinuityRecord`：连接、重连、最后消息、flush、drop、gap 和 restart 边界。

## 4. 总体实施任务清单

下列复选框保留任务原始验收范围，不用“代码存在”代替现场完成。当前实施状态以 §2.1、§2.2 和 §19 的验收台账为准。任何阶段只有在前一阶段所需门禁通过后才能进入下一阶段。

### Phase 0：基线、隔离与只读事实

- [ ] **RDP-DATA-000（P0）收口当前未提交 lineage 修复**
  依赖：无。
  工作：审查现有 diff、迁移、兼容性和测试；解决问题后单独提交，保持与本数据任务的变更边界。
  验收：工作区能够明确区分“已提交 lineage 修复”和“历史数据任务”；旧 intent 未被猜测性回填。

- [ ] **RDP-DATA-001（P0）建立操作前安全基线**
  依赖：RDP-DATA-000。
  工作：记录 Git HEAD、schema ledger、目标 profile、目标 research DB 标识的脱敏摘要；确认所有 live profile 仍禁止启动；建立数据库备份和恢复验证计划。
  验收：没有读取或输出凭证；备份目标、恢复点和停止条件明确；任何目标身份不清时停止。

- [ ] **RDP-DATA-002（P0）实现只读数据覆盖审计器**
  依赖：RDP-DATA-001。
  工作：按表、symbol、timeframe、UTC 日期、dataset version 和 ingest run 统计最早/最晚时间、行数、重复、缺口、空值、未确认 bar 和质量标记；覆盖 staging、bronze、silver、gold、meta、research 与 lineage 表。
  输出：不可覆盖的 JSON + Markdown 摘要，附查询时间、代码版本、数据库脱敏指纹和每项 SQL/算法版本。
  验收：默认只读；不能自动创建表或修复数据；同一快照重复执行结果确定；明确区分 `missing`、`zero_event_with_healthy_collector`、`collector_unknown`。

- [ ] **RDP-DATA-003（P0）生成数据恢复矩阵与容量预算**
  依赖：RDP-DATA-002。
  工作：基于实际审计结果，将每个缺口分类为“可确定性重建”“官方可回填”“第三方候选”“只能重新采集”“永远不可恢复”；下载前以单日样本估算 L2/trades 存储、网络、解析时间和数据库增长。
  验收：没有在未知体量下直接下载全量历史；每个缺口都有 owner、来源、优先级和停止条件。

### Phase 1：数据来源、schema 与不可变归档

- [ ] **RDP-DATA-010（P0）定义统一 provenance contract**
  依赖：RDP-DATA-003。
  工作：固定 `source_kind`、provider、source locator、retrieved_at、coverage_start/end、exchange timestamp semantics、schema version、dataset version、transform version、Git commit、raw SHA-256、row count、gap manifest 和 license/usage note。
  验收：`aats_ws_capture`、`okx_rest`、`okx_bulk`、`third_party`、`derived`、`proxy` 六类来源不可混淆；代理数据不能满足原始 tick 门禁。

- [ ] **RDP-DATA-011（P0）设计并迁移数据注册与归档元数据 schema**
  依赖：RDP-DATA-010。
  工作：新增或扩展 source registry、import run、archive partition、gap record、dataset bundle 和 rebuild run 表；补唯一约束、时间范围检查、外键和常用查询索引。
  验收：迁移由显式 schema job 执行并进入 checksum ledger；迁移与模型一致；具备前向、兼容和回滚策略；应用启动不执行 DDL。

- [ ] **RDP-DATA-012（P0）实现不可变分区归档**
  依赖：RDP-DATA-011。
  工作：按 dataset/symbol/UTC 日期输出 Parquet 分区；生成 manifest、SHA-256、行数、最小/最大时间、sequence 范围和 gap 摘要；使用临时文件、fsync 和原子 rename。
  验收：已有目标默认失败而非覆盖；随机分区可恢复并与源行数/哈希一致；失败不产生“已归档”状态。

- [ ] **RDP-DATA-013（P0）改造 archive-before-delete 生命周期**
  依赖：RDP-DATA-012。
  工作：将 retention 改为 `DISCOVERED -> ARCHIVING -> VERIFIED -> DELETE_ELIGIBLE -> DELETED`；只有相同范围归档校验成功才允许删除；删除继续要求显式 apply/confirm。
  验收：归档失败、manifest 缺失、哈希不一致、范围重叠或数据库异常时删除行数必须为零；workflow 不得通过 `allow_failure` 绕过 archive-before-delete 硬门。

- [ ] **RDP-DATA-014（P0）调整保留策略与磁盘保护**
  依赖：RDP-DATA-013。
  工作：热库保留仍按查询需求控制，但原始归档至少覆盖研究目标窗口；为磁盘剩余空间、归档积压、未归档待删数据和分区校验失败增加保护。
  验收：低磁盘时采集器/归档器给出明确退化状态，绝不静默删除未归档事实；恢复演练证明归档可重放。

### Phase 2：官方历史数据回填

- [ ] **RDP-DATA-020（P1）加固 OHLCV 回填链路**
  依赖：RDP-DATA-010、RDP-DATA-011。
  工作：复核并扩展现有 `rdp_deep_backfill_api.py`；支持 checkpoint、限速、重试、断点续跑、dry-run、已确认 bar、半开时间区间、raw response 归档和 gap report。
  验收：同一区间重复运行幂等；不写未确认 bar；不跨缺口插值；15m/1H 的时间边界和 UTC 对齐通过测试。

- [ ] **RDP-DATA-021（P1）加固资金费率历史导入**
  依赖：RDP-DATA-010、RDP-DATA-011。
  工作：保留 REST 最近窗口能力，增加官方历史文件适配；区分预测费率、当前费率和已结算费率；按实际 funding time 计算间隔。
  验收：不假定固定 8 小时周期；重复记录按稳定自然键去重；实际结算费率与预测值不混列。

- [ ] **RDP-DATA-022（P1）新增官方逐笔成交导入器**
  依赖：RDP-DATA-010 至 RDP-DATA-012。
  工作：支持 OKX 最近三个月 REST 和官方历史文件；保留 trade ID、exchange timestamp、side、price、size、source 和原始分区哈希；按来源分别落地。
  验收：跨页、跨文件和重复下载幂等；成交方向和数量单位经 fixture 验证；可确定性重建 trade flow 与 volume profile。

- [ ] **RDP-DATA-023（P1）新增官方历史 L2 导入与规范化**
  依赖：RDP-DATA-003、RDP-DATA-010 至 RDP-DATA-012。
  工作：先完成 1 日样本，再扩展 30 日和目标 90 日；原始 L2 单独存储；规范化 1 Hz/2 Hz 时只使用采样点之前最近状态并设置最大 staleness，记录重采样规则和 gap。
  验收：无未来数据；序列连续性、价格档位、数量单位和适用于该来源版本的完整性证据可验证；当前 OKX 协议以 `seqId`/`prevSeqId` 为主，2026-06-23 起固定为 `0` 的废弃 payload checksum 不得被当作 PASS，raw 分区 SHA-256 始终强制；输出明确标为 `okx_bulk_l2_resampled`，不得标为 `aats_ws_capture`。

- [ ] **RDP-DATA-024（P1）新增历史 mark-price candle 导入器**
  依赖：RDP-DATA-010、RDP-DATA-011。
  工作：通过官方历史 mark-price candle 接口获取 15m/1H bar，作为独立 proxy 数据集。
  验收：字段和 UI 明确显示 `bar proxy`；不得写入 live mark tick 的原始来源；不得通过 live collector freshness 门。

- [ ] **RDP-DATA-025（P1）建立自有执行事实只读恢复与对账**
  依赖：RDP-DATA-000、RDP-DATA-001。
  工作：在明确授权和现有账户只读边界内，将本地订单、成交、账单与交易所可提供的历史进行对账；不在研究库保存凭证。
  验收：只恢复订单/成交/费用事实；无法恢复的 intent、审批、风控原因保持 `unattributable`；不触发下单或账户状态变更。

### Phase 3：不可回填数据的持续重新采集

- [ ] **RDP-DATA-030（P0）修正采集器事实与文档漂移**
  依赖：RDP-DATA-000。
  工作：将 `trades-all` 说明修正为实际 `trades`；删除历史强平 REST“保留七天”的错误陈述；同步当前频道、VIP、采样和来源边界。
  验收：代码常量、订阅测试、模块说明、RDP 文档和 runbook 一致。

- [ ] **RDP-DATA-031（P0）加固实时 OI/mark/trades/BBO/books5 采集**
  依赖：RDP-DATA-010 至 RDP-DATA-014。
  工作：同时保存 exchange event time、local received time、sample time、payload sequence、connection generation 和 ingest run；明确 client-side 1 Hz/2 Hz 采样语义。
  验收：重连不会伪造连续性；DB outage、flush failure、hard-cap drop 均形成 gap record；采集器不能在有 drop 时报告 succeeded。

- [ ] **RDP-DATA-032（P0）加固公共强平实时采集**
  依赖：RDP-DATA-010 至 RDP-DATA-014。
  工作：持续订阅官方 `liquidation-orders`，保留事件原文哈希、方向语义、exchange timestamp、连接代次和 gap；强化 burst/DB outage 行为。
  验收：零事件只有在 collector continuity 健康时才解释为有效零；未知健康状态不能写成零强平；自然键去重和 side 语义测试通过。

- [ ] **RDP-DATA-033（P1）建立采集连续性账本**
  依赖：RDP-DATA-031、RDP-DATA-032。
  工作：按 channel/symbol/connection generation 保存 connect、disconnect、reconnect、last message、flush、drop、clock skew 和 shutdown 终态。
  验收：任意 15 分钟窗口可以回答“完整”“有已知缺口”“运行状态未知”；不能用进程当前健康覆盖历史窗口。

- [ ] **RDP-DATA-034（P1）让 live 采集自动进入归档链路**
  依赖：RDP-DATA-012、RDP-DATA-031 至 RDP-DATA-033。
  工作：将到期 live 原始数据分区归档，验证后再进入热库 retention。
  验收：连续运行、重启、重复归档和跨日边界均幂等；归档延迟和积压可监控。

### Phase 4：双准入门禁与派生层重建

- [ ] **RDP-DATA-040（P0）保留并强化 live capture eligibility**
  依赖：RDP-DATA-031 至 RDP-DATA-033。
  工作：保留当前采样数、funding/mark、collector freshness、lineage 和质量标记硬门；将 connection generation、gap 和 drop 证据纳入 fingerprint。
  验收：任何缺口、drop、陈旧、版本不一致或 heartbeat 未知均失败关闭；稀疏强平的有效零语义保持不变。

- [ ] **RDP-DATA-041（P0）新增 historical research eligibility**
  依赖：RDP-DATA-020 至 RDP-DATA-024。
  工作：以官方来源、checksum、覆盖率、gap、时间因果、重采样规则、schema/transform version 和 bundle fingerprint 判定历史资格，不要求伪造 live collector heartbeat。
  验收：不同来源有独立 ingest lineage；通过统一 `dataset_bundle_id` 组合，但不伪造相同 ingest run；proxy 和第三方数据有明确限制。

- [ ] **RDP-DATA-042（P0）实现数据源兼容矩阵**
  依赖：RDP-DATA-040、RDP-DATA-041。
  工作：定义哪些来源可以进入 OHLCV research、microstructure research、L2 replay、live calibration 和 capital eligibility。
  验收：历史 L2 可进入研究/L2 replay，但不能证明 live collector 完整；mark candle proxy 不能满足 tick 级门；第三方强平/OI 默认不能成为生产真相。

- [ ] **RDP-DATA-043（P0）确定性重建 Silver/Gold**
  依赖：RDP-DATA-041、RDP-DATA-042。
  工作：从合格 bundle 重建 orderbook metrics、trade flow、OI/funding metrics、volume profile、liquidation metrics、Gold replay bars、quality reports 和 artifact index。
  验收：同输入 bundle、代码版本和配置得到相同 fingerprint；半开时间区间和 UTC 对齐正确；禁止未来数据、跨已知缺口 forward-fill 和隐式插值。

- [ ] **RDP-DATA-044（P1）完成旧窗口重建分类**
  依赖：RDP-DATA-043。
  工作：对审计发现的旧窗口逐一标记 `rebuilt_exact`、`rebuilt_external_source`、`proxy_only`、`cannot_recover` 或 `awaiting_live_collection`。
  验收：不存在含混的“已补齐”；每个窗口可追踪输入 source、bundle、transform 和结果 artifact。

### Phase 5：归因、Research Factory 与执行现实性

- [ ] **RDP-DATA-050（P0）复验精确 intent lineage 与 readiness**
  依赖：RDP-DATA-000、RDP-DATA-043。
  工作：使用真实新 intent 验证 family/symbol/timeframe/signal bar/market as-of/parameter set/runtime generation/code version；旧 intent 保持不可归因。
  验收：零精确对齐、live 查询失败、lineage 缺失、数据 bundle 不合格均不能通过 Phase 3/Phase 6 readiness。

- [ ] **RDP-DATA-051（P1）扩展 Research Factory 数据源契约**
  依赖：RDP-DATA-042 至 RDP-DATA-044。
  工作：让数据源显式携带 source/bundle/eligibility；扩展字段前先定义经济语义、单位、缺失处理和时间可见性。
  验收：普通 OHLC/funding 研究兼容；微观结构字段只在所需时连接；source fingerprint 变化会改变 experiment fingerprint。

- [ ] **RDP-DATA-052（P1）重新预注册并运行 development campaign**
  依赖：RDP-DATA-051。
  工作：在看到结果前固定机制、窗口、成本、容量、缺失率、walk-forward、bootstrap、多重检验和失败条件；先使用连续 30 日，数据允许时扩展到目标 90 日。
  验收：全部计划和失败进入 trial count；train/valid/test 隔离；holdout 不因历史补数而重新开放；候选失败必须如实淘汰。

- [ ] **RDP-DATA-053（P1）重新运行 L2 event replay 与 paper calibration**
  依赖：RDP-DATA-023、RDP-DATA-052。
  工作：使用合格历史 L2 做队列、partial/no-fill、spread、impact、latency 和容量压力；用模拟订单生命周期做校准。
  验收：历史 L2 replay、OHLCV bar proxy 和实际模拟成交三类证据分别展示；任何一类不得冒充真实撮合或真实收益。

### Phase 6：API、UI、监控与运维

- [ ] **RDP-DATA-060（P1）新增统一数据覆盖读模型/API**
  依赖：RDP-DATA-002、RDP-DATA-040 至 RDP-DATA-044。
  工作：提供版本化快照，返回各数据集来源、覆盖、缺口、freshness、archive、eligibility、最近 import/rebuild run 和下一行动。
  验收：页面只读一个统一快照；未知、缺失、有效零和失败状态不会被混淆；API 不返回 DSN 或敏感字段。

- [ ] **RDP-DATA-061（P1）重构 RDP 数据治理 UI**
  依赖：RDP-DATA-060。
  工作：增加“数据覆盖”“历史导入”“实时采集”“归档”“质量资格”“重建”视图；支持 dry-run、受控触发、取消、重试和查看原因。
  验收：所有文案为 UTF-8 中文；任务状态、阶段、进度、可运行时间、停止原因和重试关系清晰；按钮不能直接触发 live 或参数应用。

- [ ] **RDP-DATA-062（P1）补齐指标、告警与审计**
  依赖：RDP-DATA-013、RDP-DATA-031 至 RDP-DATA-034、RDP-DATA-060。
  工作：监控 channel freshness、gap、drop、flush failure、archive backlog、disk、import rate、checksum failure、rebuild lag 和 eligibility ratio。
  验收：关键连续频道陈旧、未归档删除风险、磁盘危险、checksum 不一致和采集器退出均产生明确告警；日志不含秘密。

- [ ] **RDP-DATA-063（P1）更新现行文档与历史边界**
  依赖：对应代码任务完成。
  工作：同步 RDP README、平台/Operator/收益运行手册、数据源与恢复说明；旧设计和任务书保留历史事实并链接当前入口。
  验收：修正 `trades-all`、强平 REST 和 retention 误导；文档包含日期、Git 基线、静态/运行时边界；Markdown 链接检查通过。

### Phase 7：测试、部署与受控模拟验收

- [x] **RDP-DATA-070（P0）单元与属性测试**
  覆盖：分页、限速、checkpoint、去重、半开区间、时区、单位、checksum、原子归档、archive-before-delete、重采样无未来数据、稀疏强平、双 eligibility、bundle fingerprint、重建确定性和敏感字段脱敏。

- [ ] **RDP-DATA-071（P0）WSL2/Postgres 集成测试**
  覆盖：迁移 ledger、约束、并发 importer、崩溃恢复、归档恢复、collector→raw→silver→eligibility、历史 bundle→silver/gold、旧 schema 兼容和只读 live DB 边界。

- [ ] **RDP-DATA-072（P0）故障注入与资源测试**
  覆盖：网络断开、API 429/5xx、损坏 ZIP、磁盘不足、DB outage、进程 kill/restart、重复运行、时钟偏差、缓冲区 hard cap 和 archive checksum mismatch。
  验收：所有故障均可恢复或明确失败；不得产生静默数据丢失、重复事实或误报 succeeded。

- [x] **RDP-DATA-073（P0）代码审查与完整静态回归**
  工作：按 correctness、edge cases、security、performance、maintainability、test coverage 和金融正确性复审全部改动。
  验收：Ruff、受影响测试、完整 Windows unit 和最窄 WSL2 integration 实际通过；未运行项明确为未知。

- [x] **RDP-DATA-074（P0）标准 derivatives 模拟部署**
  依赖：RDP-DATA-070 至 RDP-DATA-073，且代码已提交。
  工作：只通过 `bash scripts/deploy.sh --profile derivatives --skip-commit` 部署；验证应用容器、RDP、两个 collector、schema、系统健康、恢复和数据 freshness。
  验收：不运行任何 live profile；`/healthz` 之外完成分层健康验证；simulation evidence 继续保持 `production_ready=false`。

- [ ] **RDP-DATA-075（P0）受控历史导入与连续采集验收**
  依赖：RDP-DATA-074。
  工作：按“1 日样本 → 30 日研究窗 → 目标 90 日”的门逐级导入，不一次性全量；启动并观察不可回填频道。
  验收：每一级先核对容量、checksum、gap、Silver/Gold 重建和 UI，再批准下一级；连续性不足时等待真实采集，不伪造通过。

- [x] **RDP-DATA-076（P0）完整 RDP 模拟运行与结果应用隔离**
  依赖：RDP-DATA-050 至 RDP-DATA-075。
  工作：手动触发完整 RDP，监控 Run/Attempt/Step/Event、数据门、归因、研究、L2 和 readiness。
  验收：研究 recommendation 可以生成和审阅，但本任务不自动 apply；只有候选、统计、执行、归因和故障门全部通过，才另行提出参数应用计划；任何 NO-GO/UNKNOWN 保持失败关闭。

## 5. 输入/输出接口

### 5.1 输入

- OKX 官方公开 REST、WebSocket 和历史文件；
- 当前 RDP research DB；
- 主交易模拟库的受控只读事实；
- 受版本控制的 workflow、research campaign 和 schema；
- 操作员显式给定的 symbol、时间范围、来源和 dry-run/apply 模式。

### 5.2 输出

- 覆盖审计 artifact；
- raw archive partitions 与 manifest；
- import/archive/gap/bundle/rebuild 元数据；
- historical/live eligibility evidence；
- 可重现 Silver/Gold 与 Research Factory artifact；
- UI 统一读模型；
- 无秘密的部署、模拟运行与验收证据。

## 6. 数据库、索引与约束原则

1. 研究和历史导入只写 RDP research/governance 数据库；主交易库只读。
2. 原始事实与派生事实分表或至少以不可混淆的 source contract 分区。
3. 自然键包含足以防止跨来源误去重的 source identity。
4. 时间范围必须满足 `coverage_end > coverage_start`；所有时间统一存 UTC。
5. import/archive/rebuild 使用稳定幂等键；非终态同范围并发运行由唯一约束或 advisory lock 阻止。
6. migration 必须包含模型、SQL、ledger checksum、集成测试和兼容说明；常规回滚优先停止新 writer、保留已采集事实。

## 7. 事务、一致性与并发

- raw file 下载到临时路径，校验完成后原子发布；
- 元数据只有在原始文件、checksum 和范围验证成功后进入 verified；
- 数据库批次失败必须回滚当前批次并保留 checkpoint，不能将部分写入标记为 succeeded；
- 同一 dataset/source/symbol/window 只允许一个非终态 import/rebuild；
- archive verified 与热数据 delete eligibility 在同一可审计状态机推进；
- 任何并发重试必须通过稳定 operation ID 合并，不产生重复 Run 或重复事实。

## 8. 授权、认证与数据安全

- 公共市场数据导入不需要交易凭证；
- 账户历史对账只使用现有受控只读运行边界，另行记录授权和目的；
- CLI 参数、进程列表、日志、artifact 和 UI 不得出现完整 DSN、cookie、token 或账户标识；
- API 受现有 Operator session、权限和审计保护；危险动作必须 dry-run 优先并要求显式确认；
- live profile 禁用逻辑、Kill Switch、人工审批和双签边界保持不变。

## 9. 错误处理、幂等与停止条件

以下任一条件必须停止当前阶段：

- 目标数据库或 profile 身份不明确；
- 备份/恢复计划未验证；
- 来源不是官方或无法证明 provenance；
- 下载文件 checksum、schema 或时间范围不符合预期；
- 发现时间穿越、单位不一致、重复自然键冲突或不可解释 gap；
- 磁盘空间低于安全水位；
- archive 未验证却准备删除热数据；
- live collector 有 drop/flush failure 却报告 succeeded；
- 需要读取、打印或复制凭证才能继续；
- 任一动作可能触发真实资金、live profile 或参数应用。

## 10. 状态转换与生命周期

Historical import：

```text
PLANNED -> DOWNLOADING -> DOWNLOADED -> VERIFIED -> LOADING
        -> LOADED -> REBUILDING -> SUCCEEDED
        \-> FAILED_RETRYABLE | FAILED_TERMINAL | CANCELLED
```

Archive partition：

```text
DISCOVERED -> ARCHIVING -> VERIFIED -> DELETE_ELIGIBLE -> DELETED
           \-> FAILED
```

Data gap：

```text
OPEN -> CLASSIFIED -> BACKFILLED | REBUILT | AWAITING_LIVE_COLLECTION
                    -> CANNOT_RECOVER | THIRD_PARTY_ONLY
```

任何终态都保留证据；`CANNOT_RECOVER` 不能被无来源的数据改写为 `BACKFILLED`。

## 11. 缓存、性能与容量

- 所有历史导入流式解析并分批写入，禁止将全量 L2 无界载入内存；
- 下载、解析、落库和派生分别记录吞吐、CPU、内存、磁盘和估计完成时间；
- 大表查询使用 symbol/time 范围索引，避免无界 COUNT 和全表排序；
- UI 读取预聚合覆盖快照，不在请求路径扫描原始 tick 表；
- 先用 1 日样本校准容量，再决定 30/90 日扩展；容量不足时停在较小范围并报告，不降低数据完整性。

## 12. 日志、监控与审计

必须记录：operation ID、source、symbol、时间范围、阶段、行数、checksum、dataset/bundle version、Git/schema/transform version、重试次数、gap、drop、错误原因和输出 artifact。

禁止记录：完整 URL 查询中的秘密、DSN、authorization header、cookie、token、API key、密码和未脱敏账户标识。

## 13. 测试策略

1. 单元测试：解析、单位、时区、分页、去重、checkpoint、checksum、状态机、门禁和脱敏。
2. 属性测试：半开区间、无未来数据、重采样、幂等、重复/乱序消息和边界数值。
3. 集成测试：真实 PostgreSQL schema、约束、事务、advisory lock、归档恢复和完整数据流。
4. 故障测试：网络、API、磁盘、DB、进程、时钟、缓冲区和损坏文件。
5. 回归测试：Ruff、相关测试、完整 Windows unit、最窄 WSL2 integration。
6. 模拟运行：标准 derivatives 部署、公共 collector、RDP workflow、UI 和 readiness；不触碰 live。

## 14. Migration、Rollback 与兼容

- 新增 schema 优先采用向后兼容的表/可空列/索引；不重写旧原始事实；
- source-aware 唯一键变化必须先做冲突审计，再迁移；
- 回滚应用代码时保留已导入和已归档数据，避免二次不可逆损失；
- 旧 Silver/Gold/artifact 保留其原始版本和资格状态，不原地包装成新 bundle；
- 历史 API/CLI 保持兼容或返回明确迁移错误，不静默改变数据目标；
- 每个 migration 提供隔离 PostgreSQL 前向验证；破坏性 rollback 必须停服并另行批准。

## 15. 配置与环境隔离

- Windows 用于代码、静态检查和单元测试；
- WSL2 只使用 `derivatives` 模拟 profile 做 Postgres/collector/部署验证；
- live profile 继续在任何同步、构建、停服、迁移或网络副作用前失败；
- 历史下载目录、归档目录、临时文件和 research DB 必须显式配置并校验绝对路径；
- 配置键必须属于 `AATSSettings.model_fields`，禁止添加无消费者伪开关或用 `extra=ignore` 隐藏错误。

## 16. 代码组织与依赖

优先复用：现有 backfill、ingest run、schema ledger、microstructure collector、Silver merger、quality、Research Factory、RDP Run/Step/Event 和 Workspace V3 读模型。

新增适配器按 source 类型隔离；共享 provenance、checkpoint、下载、checksum、gap 和状态机组件，不复制业务规则。任何新依赖必须进入 lock、供应链和许可证检查。

## 17. 文档与运维手册

实施后至少更新：

- `aats/data_platform/README.md`；
- `docs/rdp/README.md`；
- `docs/operations/platform_runbook.md`；
- `docs/operations/profit_readiness_runbook.md`；
- `docs/operations/rdp_operator_workflow.md`；
- 数据恢复、归档、collector 连续性和故障处理专题 runbook；
- `docs/testing/profit_readiness_acceptance.md`；
- 对应 migration、API 和 UI 参考。

旧任务书保留历史结论；当前实现变化必须更新现行入口，不用历史测试通过数证明今天的运行状态。

## 18. 最终部署与验收标准

只有以下条件全部满足，才能把本任务标记为工程完成：

1. 当前 lineage 修复已独立复审、验证和提交；
2. 当前数据库覆盖矩阵有只读、带时间和基线的证据；
3. 所有可回填来源可断点续跑、幂等、校验和验证并有 gap manifest；
4. 所有不可回填来源已持续采集且 continuity 可查询；
5. 未归档数据不能被 retention 删除，归档可恢复；
6. historical/live eligibility 分离且均失败关闭；
7. Silver/Gold 重建无未来数据、无隐式插值并可确定性复现；
8. 旧 intent 和历史运行证据未被猜测性回填；
9. API/UI/监控能够如实展示来源、覆盖、缺口、代理、未知和失败；
10. Ruff、完整 unit、最窄 WSL2 integration、故障测试和标准 derivatives 模拟部署按实际执行并通过；
11. 完整 RDP 可在模拟环境完成或以明确 NO-GO/UNKNOWN 失败关闭；
12. 所有 live profile、真实订单和自动参数应用保持未执行。

即使工程验收全部通过，也只代表“数据与研究链路具备可信运行条件”，不代表候选盈利、真实资金安全或 production-ready。盈利与资本资格必须由独立预注册研究、OOS、执行校准、模拟观察和后续人工门禁决定。

## 19. 实施验收台账（2026-08-26）

| 任务 | 工程状态 | 现场状态 / 未完成边界 |
| --- | --- | --- |
| 000 | 已完成并独立提交 `c1b015ec`；新 intent 已在完整模拟 RDP 中复验 | 精确 replay/live 对齐仍为 0，readiness 保持 NO-GO；不能猜测补写 lineage |
| 001–003 | 审计器、恢复矩阵和容量字段已实现；目标库 v5 覆盖快照已生成 | 单日实际导入容量、备份/恢复点和随机恢复仍待现场生成；未执行前不得写“完成” |
| 010–011 | contract、registry、Stage 18 与 ORM 已完成；隔离 PostgreSQL 和目标模拟库 migration ledger/schema guard 均通过 | 后续每次来源/导入仍须按 registry 和 bundle 约束生成证据 |
| 012–014 | 不可变 Parquet、resume、archive-before-delete、磁盘保护与 retention 硬门已完成 | 随机真实分区恢复、跨日连续运行和归档卷容量待现场验收 |
| 020–024 | confirmed candle、funding、trade REST/file、L2 file、mark bar proxy 适配器已完成并定向测试 | 尚未导入官方 1 日样本；无官方 L2 文件时 023 保持 `awaiting_source`；30/90 日未获容量批准 |
| 025 | 未实施，且未伪造 | 需要账户只读授权；旧 intent 继续 `unattributable` |
| 030–033 | 频道事实、连接代次、MESSAGE/FLUSH/DROP/SHUTDOWN、gap 和 run 终态已完成；部署后两个 collector 健康且持续 flush trades/OI/funding/BBO/books5/liquidation | 重连、DB outage、进程 kill/restart、跨日连续观察与有效零故障演练仍待执行 |
| 034 | archive 已成为 data maintenance 中 retention 的不可绕过前置硬门 | 真实到期分区与跨日运行待观察 |
| 040–042 | live/historical 双准入、连续性 fingerprint 与来源兼容矩阵已完成 | 现场 bundle/窗口必须继续按真实证据判定，不提供 override |
| 043 | 部分完成：bundle-scoped historical orderbook/trade-flow Silver 重建与运行 ledger 已完成 | OI/funding/liquidation/Gold、quality report 和 artifact index 的 source-aware 重建尚未完成；不得声称完整派生层恢复 |
| 044 | 分类 contract 与 gap 状态机已完成 | 尚未对目标库旧窗口逐一生成分类结果和 artifact，现场状态未完成 |
| 050–053 | lineage 前置失败关闭已完成；完整 RDP 对四个组合均生成 `pause`，未应用参数 | replay/live 精确对齐为 0；campaign、执行事实和资本资格依赖 30/90 日数据与只读对账，当前保持 NO-GO/UNKNOWN |
| 060–063 | coverage artifact、API、Workspace UI、可靠性告警和现行 runbook 已完成；部署后 bounded snapshot 与治理 DB 快照已验证 | 重启后既有签名会话失效；页面视觉与真实告警交互需重新登录补验 |
| 070 | 单元、半开边界、解析、幂等、连续性、归档与双准入测试已通过 | 无 |
| 071 | Stage 18 隔离 PostgreSQL 前向、幂等、回滚、修复和约束验证通过 | collector→raw→silver→eligibility 与归档恢复的完整数据库集成仍待现场 |
| 072 | 429/5xx、损坏输入、磁盘水位、buffer hard cap、drop、checksum 和重复运行等定向故障测试已实现 | 真实进程 kill/restart、DB outage 和资源压测待模拟现场 |
| 073 | 完整静态回归与本次改动代码审查已完成 | 无新增静态阻断 |
| 074 | 标准 derivatives 模拟部署已完成；generation 与 evidence 已固化 | 本状态不是持续证明，后续部署必须重新核验 |
| 075 | 持续采集已在模拟部署中运行且目标库覆盖快照已生成 | 官方 1 日样本、30/90 日扩展、随机归档恢复和故障注入未执行，因此仅部分完成 |
| 076 | 完整 RDP 10/10 步骤已完成，结果按归因门失败关闭；recommendation 全部为 `pause`，未 apply | 数据/归因缺口仍是业务 NO-GO，不得将“流程退出码 0”误写为“研究通过” |

本台账明确区分“已实现且静态/隔离验证通过”和“目标运行环境已证明”。后者只能由同一日期、同一 commit、同一 profile 的现场证据更新。

## 20. 最终模拟现场证据（2026-08-26）

### 20.1 代码、部署与运行健康

- 实现提交：`fe5596fd5ee4`（此前依次为 `c1b015ec`、`42d2e14e`、`d222e291`、`1e0420fd`）；
- 标准部署 generation：`fe5596fd5ee4-20260826T151737Z-392-3966`；
- 部署 evidence：`/root/aats/deploy/wsl2-dev/runtime/deployment-evidence/20260826T151904172220Z-derivatives-fe5596fd5ee4.json`；
- 最终复核时，Gateway、Market、Decision、Execution、RDP daemon、microstructure collector、liquidations daemon 均为 `healthy`，重启计数为 0；`/healthz` 返回 `status=ok`。最近 10 分钟应用日志未匹配 `Traceback`、`CRITICAL`、`Unhandled`、`OperationalError`、`IntegrityError` 或非零进程退出；
- kill switch 保持 `HALTED`，原因为 `trial_guard_threshold_breached`，`resume_authorized=false`；本任务未恢复它，也未启动 live profile、提交真实订单或应用参数。

### 20.2 覆盖审计事实

- v5 JSON artifact：`/app/artifacts/data_governance/coverage/coverage_20260826T151922950145Z.json`，SHA-256 `53672eb8f548cc41472d1082d5e793b4d721b0238bedc6a2f7bdee55d96b3607`；
- v5 Markdown artifact：`/app/artifacts/data_governance/coverage/coverage_20260826T151922950145Z.md`，SHA-256 `3afa41f3aae7b4c8a910d00dd043bb35f0f59e370278c9b8549a423c424196c4`；
- 98 个 dataset 的状态为：`missing=47`、`observed=26`、`observed_with_quality_issues=23`、`unbounded_not_scanned=2`、`audit_failed=0`；
- 恢复分类为：`cannot_recover=1`、`deterministic_rebuild=22`、`official_backfill=26`、`prospective_only=21`；其余为空或仅待观测，不能把分类数解释为已恢复数；
- `bronze.market_orderbook_payloads` 在同一 repeatable-read 快照中统计为 253,525 行，symbol 非空行 253,525，自然键重复 0。v5 同时消除了 `meta.ingest_runs` 可空 symbol 和无时间治理表主键的误报。

### 20.3 完整 RDP 与真实阻断

- task：`task_235c5e4eb2a7`；run：`run_ff3e022b420444f7`；排队于 23:07:36、约 7 秒内开始，未等待五分钟轮询；10 个步骤均进入完成态；
- Phase 3：`20260826_150925_f175fd1b`，`replay_only=false`、`live_query_succeeded=true`；Phase 4：`20260826_150933_12ddbb91`；decision round：`20260826_150943_630c50bf`。决策读取的 Phase 3/4 ID 与本轮 DB 当前快照严格一致，不再误用旧 JSON 快照；
- 最终结果为 `blocked_by_attribution` / `not_ready_attribution_issue`。四个策略/周期组合均为 `aligned=0`、`live_only=0`；总计 `unattributable=5398`，其中 15m replay-only 5,390、1h replay-only 2,160（不同组合会复用窗口，不能相加为独立样本数）；
- 系统生成 8 条 recommendation，四个组合的系统动作均为 `pause`；没有执行 approve、release、apply 或 rollback。流程技术成功只证明门禁能正确阻断，不证明收益、资本资格或 production-ready。

### 20.4 尚未完成且不能由软件伪造的事项

1. 尚未执行官方 1 日历史样本，因此也未批准 30/90 日扩展；尚无本任务可用的官方 L2 文件；
2. 尚未执行随机 archive 恢复、真实到期 retention、DB outage、网络重连、进程 kill/restart、磁盘压力和跨日连续观察；
3. 47 个 dataset 仍缺失，23 个已有数据但存在质量问题；其中 `cannot_recover` 与 `prospective_only` 只能保留真实缺口或从现在持续采集；
4. 服务重启后原浏览器登录会话失效，签名 RDP 页面视觉验收仍需操作员重新登录；后端运行、任务队列、DB 快照和 collector 已完成现场验证；
5. 精确归因、候选经济性、执行校准、L2/paper forward、一次性 holdout 和 fault matrix 尚未同时通过，production/trading readiness 固定为 NO-GO。
