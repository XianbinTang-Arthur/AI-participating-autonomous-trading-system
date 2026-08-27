# RDP 连续性监控与研究资格语义 LF-B 任务书

> 文档状态：`BLOCKED_PENDING_HUMAN_LF_B_APPROVAL`
> 编写与最后核对：2026-08-27（代码基线 `6dd75ab01381f5372e9a60117b83ed21079c5103`）
> 核对范围：连续性 schema、两个 WebSocket collector、`classify_continuity_window`、数据治理只读投影及现有单元契约
> 运行时边界：本文是待审批设计，不证明当前容器、数据库、交易所连接、频道新鲜度或数据完整；未部署、未访问凭证、未访问外部网络、未触发交易或参数应用
> 阻断原因：方案需要新增/迁移 PostgreSQL schema、改变 collector 写入协议和研究资格判定；属于 LF-B，必须由真人批准迁移、兼容、回滚和现场窗口后才能实施

## 1. 决策摘要

本问题不能通过在 API 中排除 `liquidation-orders` 的某些 symbol、取消一个 `LIMIT 100`，或硬编码
七个 `collector/channel/symbol` scope 正确解决。原因是当前账本没有保存权威的“应订阅什么、实际请求
了什么、交易所确认了什么”：

- 强平订阅的真实身份是 `channel=liquidation-orders + filter_kind=inst_type + filter_value=SWAP`，
  不是每个返回 `instId` 一个订阅；
- 微观结构 daemon 支持 CLI 扩展 symbol，静态默认值不能代表实际运行清单；
- `trades`、BBO、books5、OI、funding、mark 和 liquidation 的活动模型不同，不能共用“120 秒内必须
  有业务消息”的规则；
- 当前 lifecycle 和 summary 都只看最近 24 小时；已有 observed group 超窗后会从投影消失，而从未
  注册/产生日志的 expected subscription 从一开始就不可见，历史 DISCONNECT 又可能被误当成当前断连；
- `classify_continuity_window` 以窗口内首尾 `MIN/MAX(MESSAGE)` 接近边界作为 complete 的核心证据，
  无法证明中间桶无缺口，属于会影响研究准入的 P0 语义缺口。

因此本任务升级为 schema-backed LF-B：先建立 typed subscription catalog、运行期实际注册与交易所 ACK
证据，再切换监控和研究资格。审批前不得上线静态补丁。

## 2. 当前事实、问题与不可静默边界

### 2.1 已确认的代码事实

1. `_live_collection` 与 `_monitoring` 分别按 `collector/channel/symbol` 聚合，并在聚合后按字母顺序
   `LIMIT 100`；两者可能读取到不同快照，且 summary 只覆盖返回的前 100 个 scope。
2. `_monitoring` 把最近任意 continuity event 的 age 统一与 120 秒比较；FLUSH/CONNECT 可掩盖
   streaming observation 陈旧，稀疏强平又会因没有逐 symbol 事件被误报。
3. 24 小时内任一 DISCONNECT 都会持续产生 critical，之后的 RECONNECT 不能清除“当前断连”。
4. liquidation daemon 订阅 `instType=SWAP`，但 market-data、FLUSH 与 DROP 会按实际 `instId` 写入
   continuity；两种身份被错误混在同一 `symbol` 维度。
5. microstructure daemon 的实际 symbol 来自 CLI；代码默认仅是默认值，不是运行期权威事实。
6. DROP continuity 与 `data_gap_records` 当前由相邻但独立的数据库写入完成；进程在两次写入之间失败时，
   可能出现 DROP 有记录但 gap 缺失，或重试后的重复/不一致。

### 2.2 “95 条告警”的处理

本轮只读运行证据如下；它是本文的时点证据摘要，不是永久运行事实：

| 证据 | 精确范围与结果 |
| --- | --- |
| continuity 聚合 | 2026-08-27 18:45:01.547284+08，WSL2 Ubuntu、`aats_research`；按最近 24 小时 `collector/channel/symbol` 聚合得到 `total_observed_group=302`、`age_seconds>120 group=292`、`drop_scope=5`、`drop_event=10`、`disconnect_event=7` |
| reliability 输出 | `run_20260827_101511_d7eebe`，2026-08-27 18:15:11--18:15:16+08；旧投影报告 `95 active alert(s)`，workflow 为 `degraded` |
| 组件边界 | derivatives 模拟栈相关容器当次均为 Docker `healthy`，Gateway `/healthz` 仅返回 `status=ok/process_role=gateway`；该接口没有提供 deployment generation，因此本轮 generation 保持 `unknown` |

`302/292/5/10/7` 来自上述单次查询，执行后即可能漂移；`95` 只对应所列 reliability run。特别是
`292` 是陈旧 observed group 数，不是 DROP scope、DROP event 或 dropped rows。当前账本只证明
`drop_event=10`；`dropped_row_count` 未在本轮独立验证，保持 `UNKNOWN_NOT_VERIFIED`。

该 `95` 是旧模型产生的监控伪影，不能解释为 95 个真实当前故障，也不能因为已知误报而在 UI/API
中静默归零或删除历史证据。在 LF-B 切换完成前：

- 该数值必须标记为 `legacy_projection_untrusted`；
- 不得用它证明 collector 健康或不健康，不得触发数据资格放行；
- 现场处置必须直接核对 lifecycle、DROP/gap 和 daemon 状态；
- 新投影上线时保留旧值、算法版本与切换时间，形成可审计的 before/after，而不是改写历史。

## 3. 业务目标与范围

### 3.1 目标

1. 以数据库中的版本化 catalog 表达 typed subscription identity、活动模型和 SLA；
2. 让标准默认、daemon 实际注册、交易所 SUBSCRIBE ACK 三方可对账；
3. 让最新 lifecycle 跨越 24 小时查询窗仍可确定，历史 incident 单独计数；
4. 对 continuous/periodic/sparse/event-driven 频道使用不同完整性规则；
5. 用封闭时间桶、序列和 DROP/gap 证据证明窗口连续，而不是只看首尾消息；
6. 先对完整集合计算 summary，再对详情进行明确、有序、可分页的限制；
7. `_live_collection`、`_monitoring` 和研究资格共享同一版本化投影；
8. 所有无法归因的 legacy event 保持 unknown，不伪造 subscription identity。

### 3.2 非目标

- 不改变交易、风控、订单、参数应用或 live profile 门禁；
- 不把进程 heartbeat 当作频道 ACK、业务观测或窗口完整性；
- 不以静态 Compose 默认值冒充运行期实际 CLI；
- 不为 legacy 广义强平 symbol 猜测独立订阅；
- 不在 LF-B 审批前创建迁移、写代码、部署或清除现有告警。

## 4. 领域模型与模块职责

### 4.1 Typed subscription identity

`subscription_id` 是稳定 UUID，身份至少包含：

| 字段 | 语义 |
| --- | --- |
| `collector_role` | `aats-microstructure-collector` / `aats-liquidations-daemon` |
| `venue` | 当前为 `OKX` |
| `endpoint_kind` | public/business 等逻辑连接类型 |
| `channel` | trades、bbo-tbt、books5、open-interest、funding-rate、mark-price、liquidation-orders |
| `filter_kind` | `inst_id`、`inst_type`；禁止用空泛 symbol 代替 |
| `filter_value` | 如 `BTC-USDT-SWAP` 或 `SWAP` |
| `activity_model` | `continuous`、`periodic`、`sparse`、`event_driven` |
| `transport_sla_seconds` | 连接/帧存活边界 |
| `observation_sla_seconds` | 业务观测边界；sparse 可为 NULL |
| `bucket_seconds` | 完整性桶宽；仅适用模型填写 |
| `sequence_policy` | `none`、`collector_monotonic`、`exchange_linked` 等 |
| `policy_version` | SLA/完整性算法版本 |

强平返回的 `instId` 是 observation dimension，不是 subscription identity。OI/funding 的具体 activity
model 和 SLA 必须基于当前 OKX 协议、采集实测分布及 Risk/Data Reviewer 签字确定，禁止沿用统一 120 秒。

### 4.2 三方 inventory

1. **Default expectation**：版本控制的标准部署 manifest 写入 expectation 表，说明 profile/service 预期清单；
2. **Registered actual**：daemon 在解析最终 CLI 后注册本次 ingest run 的实际清单，保存脱敏参数摘要；
3. **Exchange acknowledged**：WebSocket 发送订阅并收到对应 ACK/ERROR，按 subscription_id 写事件。

监控状态来自三方对账，而不是任选一方：

- expected 但未 registered：`expected_not_registered`；
- registered 但未 sent：`registered_not_sent`；
- sent 但未 ACK 且超 ACK SLA：`subscribe_ack_missing`；
- ACK ERROR：`subscribe_error`；
- ACKED 且 transport/observation/bucket 符合模型：healthy/quiet；
- 非默认 CLI 扩展：标记 `registered_extension`，可监控但不自动提升为标准 required expectation。

### 4.3 事件与 lifecycle

新增 typed event：`REGISTERED`、`SUBSCRIBE_SENT`、`SUBSCRIBE_ACK`、`SUBSCRIBE_ERROR`、
`CONNECT`、`RECONNECT`、`DISCONNECT`、`SHUTDOWN`、`FRAME`、`OBSERVATION`、`BUCKET_CLOSED`、
`DROP`。每条事件携带 `subscription_id`、`registration_id`、`ingest_run_id`、
`connection_generation`、单调 `collector_event_sequence`、UTC event/local receive time 和幂等 key。

当前状态使用 subscription state projection 中的最新合法 lifecycle transition；24 小时 incident 查询只
影响统计，不影响 current state。RECONNECT/后续 ACK 可以清除当前 DISCONNECT，但不能删除历史 incident。

### 4.4 活动模型

- `continuous`：按封闭桶验证观测存在、age、drop、sequence；超 SLA 为 critical。
- `periodic`：按 catalog cadence/grace 判断，不要求每 120 秒有消息；跨预期周期才陈旧。
- `sparse`：无业务 observation 可以是 `quiet`，但必须有 fresh transport frame、ACKED subscription、
  连续桶且无 DROP/gap。
- `event_driven`：仅凭无事件不能断言 complete；依赖 transport 与来源可证明的 sequence/bucket 合同。

## 5. 数据库 schema、表、索引与约束

LF-B 建议新增以下 schema 对象，最终命名与 migration stage 由审批决定：

### 5.1 `meta.collector_subscription_catalog`

- `subscription_id UUID PRIMARY KEY`；
- typed identity、activity model、SLA、bucket、sequence policy、policy version、definition hash；
- CHECK：合法 enum、正 SLA、sparse observation SLA 可空、filter kind/value 配对；
- UNIQUE：规范化 identity + policy version；definition hash 唯一。

### 5.2 `meta.collector_subscription_expectations`

- `expectation_id UUID PRIMARY KEY`、`subscription_id` FK；
- profile、service role、required flag、source manifest/version/hash、effective_from/to；
- 排除重叠有效期；同 profile/service/subscription 在同一时刻最多一个 active expectation。

### 5.3 `meta.collector_subscription_registrations`

- `registration_id UUID PRIMARY KEY`、`subscription_id` FK、`ingest_run_id` FK；
- actual source (`default`/`cli`/managed)、脱敏 argv/config hash、registered/ended time；
- UNIQUE `(ingest_run_id, subscription_id)`。

### 5.4 continuity event 扩展与 current state

- `meta.collector_continuity_events` 增加 nullable `subscription_id`、`registration_id`、
  `collector_event_sequence` 和 typed event constraint；扩展期允许 legacy NULL，新 writer 必须非 NULL；
- 新建 `meta.collector_subscription_state`，每个 active registration 一行，保存最后 event/sequence、ACK、
  lifecycle、frame、observation、drop/gap 与更新时间；更新必须只接受更大的 sequence；
- 索引：`(subscription_id, event_ts DESC)`、`(registration_id, collector_event_sequence)` UNIQUE、
  `(event_type, event_ts DESC)`、active expectation partial index、current state 的 status/updated_at index。

### 5.5 完整性桶与 DROP obligation

- `meta.collector_continuity_buckets`：subscription/registration、半开 `[bucket_start,bucket_end)`、frame/
  observation 数、first/last receive、sequence bounds、drop count、closed/completeness、policy version、fingerprint；
- UNIQUE `(registration_id, bucket_start, bucket_end)`，CHECK UTC 对齐、end > start、计数非负；
- `meta.continuity_drop_obligations`（若不采用同事务直接写 gap）：DROP event id 唯一、gap 状态、attempt、
  last error、resolved gap id，支持幂等 reconciler。

## 6. 事务、一致性与并发

1. daemon registration、REGISTERED 事件与 ingest run 在一个事务中创建；
2. SUBSCRIBE_SENT/ACK/ERROR 以 `(registration_id, collector_event_sequence)` 幂等；乱序/重复 ACK 不得
   回退 current state；
3. DROP 与 gap 必须二选一满足强一致：
   - 首选同一数据库事务同时写 DROP continuity 和 `data_gap_records`；或
   - 同事务写 DROP + drop obligation，reconciler 幂等创建 gap，并对未解决 obligation 告警；
4. current state 使用 compare-and-set sequence，两个 reconnect watcher 不得互相覆盖；
5. summary 在同一只读事务/一致快照内计算，live 与 monitoring 复用同一 projection version。

## 7. 完整性与研究资格算法

`classify_continuity_window` 必须从“首尾消息接近边界”升级为 bucket/sequence 证明：

1. 窗口必须由 catalog policy 下的所有封闭桶完整覆盖；开放尾桶不能用于 complete；
2. 每个桶验证该 activity model 的 frame/observation/cadence 规则；
3. sequence-enabled 频道验证无回退、无未解释跳号；无 exchange sequence 时验证 collector sequence 与
   DROP obligation，不能声称交易所级无丢包；
4. 任一 DROP、未解决 gap、SUBSCRIBE_ERROR、当前未 ACK、跨 generation 未闭合边界均阻断 complete；
5. sparse 零事件只有 ACK、transport、桶覆盖和零 DROP/gap 同时满足时才是 `valid_zero/quiet`；
6. legacy 无 subscription_id 或桶证据的窗口最多为 `legacy_unknown`，不得沿用 complete；
7. 资格 artifact 固化 subscription policy version、bucket fingerprint 和 gap fingerprint。

## 8. API、投影、总数与截断

建立单一 `SubscriptionContinuityProjection` 服务，由 `_live_collection`、`_monitoring` 和研究资格读取同一
版本。它必须分别返回：

- inventory reconciliation：default/registered/sent/acked/error/missing/extension；
- current lifecycle：基于 current-state 表，不受 24 小时事件窗消失影响；
- incident summary：窗口内 disconnect/reconnect/drop/gap 的完整聚合；
- activity status：healthy、quiet、stale、unknown、critical 及 policy/version；
- 详情分页：稳定 keyset cursor，DROP/ERROR 优先，但不以详情限制计算 summary；
- `total_count`、`returned_count`、`omitted_count`、`truncated`、`next_cursor` 和算法版本。

300 个以上 observed group、其中大多数为陈旧稀疏 observation 的合成回归中，正序/逆序 summary
必须相同；stale scope、DROP scope、DROP event 和 dropped rows 分别断言，详情可有界但必须提供
真实截断元数据，不能再由字母排序 `LIMIT` 决定是否看见故障。

### 8.1 API 与 consumer 向后兼容矩阵

LF-B 切换不得在 `rdp.data_governance.v1` 中静默改变既有字段语义：

| Consumer / 字段 | 当前兼容语义 | 新能力的安全落点 | 切换与回滚要求 |
| --- | --- | --- | --- |
| 顶层 `schema_version` | `rdp.data_governance.v1` | additive shadow 字段可留在 v1；任何既有字段改义必须发布显式 v2 | v1/v2 契约测试并存；回滚恢复 v1 read path |
| `live_collection.status` | 投影是否可读取，不是健康状态 | 新增 `health_status` | 前端先兼容双字段，不能把 degraded 健康态包装成数据库不可用 |
| `live_collection.channels/channel_count` | 当前返回切片及其计数 | 新增 `channel_total_count/returned_count/truncated/next_cursor` | v1 `channel_count` 不改义；v2 才可把 total 设为主字段 |
| `live_collection.drop_count` | 当前返回切片内的 DROP event 行数 | 新增 `drop_event_count_total`、`drop_scope_count`、`dropped_row_count` | 禁止把 event 数静默改成丢失行数；unknown dropped rows 保持 NULL/UNKNOWN |
| `monitoring.status/alerts/alert_count/critical_count` | 旧算法供 UI 和 reliability 消费 | 新投影先以算法版本化 shadow 输出 | reliability 和 UI 分别完成契约/回滚测试后才切换；旧算法证据保留 |
| RDP 前端 | 读取 channel/drop/alert 摘要 | 明确展示 inventory reconciliation、current health、incident 与 truncation | 新旧 payload fixture、空态/unknown/截断回归必须通过 |
| hourly reliability | 以 `monitoring.status` 决定 degraded | 新增显式 projection version 与受治理 cutover | 切换必须原子、可回滚，不能新旧状态混读 |

## 9. 授权、认证与数据安全

继续使用现有 Gateway session 权限。catalog、registration 与 ACK 不保存 URL、DSN、token、cookie 或原始
凭证；CLI 只保存 allowlist 后的 subscription 参数和 hash。所有迁移、backfill 与现场验证仅限批准的
derivatives 模拟范围。

## 10. 错误处理、幂等与恢复

- ACK 未匹配 subscription_id、未知 channel/filter、sequence 回退、catalog version 不匹配均失败关闭；
- daemon 重启创建新 registration，不覆盖旧 run；
- reconnect 在同 registration 下增加 generation，重新记录 SENT/ACK；
- reconciler 可从未解决 DROP obligation 恢复，不重复 gap；
- current-state rebuild 可从有 subscription_id 的 append-only events 确定性重放；
- 无法确定映射的 legacy rows 保留、标记 unknown，不删除也不猜测。

## 11. 缓存与性能

保留 UI snapshot 短缓存和数据库 statement timeout。current-state 与 bucket 表避免每次扫描全部 event；incident
summary 使用时间索引聚合；详情使用 keyset pagination，不使用 OFFSET 或先 LIMIT 后 summary。必须在实际
300 个以上 group 及更高基准下分别记录 stale/DROP 指标的 query plan、延迟、返回大小与索引命中。

## 12. 日志、监控与审计

新增指标至少包括 expected/registered/ACK 差异、ACK latency/error、current disconnected、bucket missing、
DROP obligation backlog/age、legacy unattributed、projection truncation。所有状态改变携带 subscription、
registration、policy version 和 evidence event id；旧 95 告警作为 legacy algorithm evidence 保留。

## 13. 测试策略与 PostgreSQL 验收

### 13.1 单元/属性测试

- subscription identity：`inst_id` 与 `inst_type` 不可混淆；
- default/registered/ACK 三方全部组合与乱序事件；
- continuous、periodic、sparse、event-driven 各自 SLA/quiet/unknown；
- DISCONNECT→RECONNECT→ACK 清除当前故障但保留 incident；
- 24 小时无 lifecycle event 仍从 current state 得到真实状态；
- 中间桶缺失、sequence 跳号、DROP/gap 未解决不得 complete；
- 300+ group 且 stale/DROP event/DROP scope/dropped rows 独立计数、输入顺序无关、分页与截断元数据；
- legacy unattributed 永不升级为 complete。

### 13.2 WSL2/PostgreSQL 集成

1. migration apply、幂等、rollback、checksum ledger 与 ORM/schema guard；
2. catalog/expectation 有效期、FK、UNIQUE、CHECK 和并发 registration；
3. collector fixture 完成 REGISTERED→SENT→ACK→FRAME/OBSERVATION→DISCONNECT→RECONNECT；
4. DROP 与 gap/obligation 在故障注入下无单边成功和重复；
5. bucket close、跨 UTC 边界、开放尾桶、generation 切换与确定性 fingerprint；
6. explain/analyze 验证 summary、current state 和 keyset detail 在目标规模内满足 SLA；
7. legacy backfill dry-run 输出 attributed/unattributed 数量，不写猜测映射。

### 13.3 受控模拟验收

代码提交且 LF-B migration 获批后，才可通过仓库唯一 derivatives 模拟部署入口验证实际 CLI 注册、OKX
ACK、稀疏 quiet、流式 stale、断连重连、DROP/gap 和 UI。不得启动 live profile。

## 14. Legacy backfill 与真实性边界

可确定性 backfill 仅限同时具备 ingest run、collector/channel、已知 daemon 参数来源和不歧义 filter 的事件。
强平 broad `instId` observation 只能关联到经证据确认的 `instType=SWAP` registration，不能生成每-symbol
subscription。其余旧事件保留 NULL subscription_id 并计入 `legacy_unattributed_count`；任何自动映射都需
dry-run manifest、算法版本、行数与人工批准。

## 15. Migration、切换、回滚与兼容

采用 expand → dual-write → verify → read-shadow → cutover → contract：

1. expand：新增表/nullable 列/索引，不改变旧读写；
2. dual-write：新 collector 写 typed evidence，旧投影继续可见并标 legacy；
3. verify：PostgreSQL 集成、backfill dry-run、shadow diff 和实际 ACK 证据通过；
4. read-shadow：新旧 summary 并列，解释 95 等差异；
5. cutover：API 切到新 projection，保留算法版本和旧审计入口；
6. contract：观察窗和 rollback window 结束后，另案批准 new-write NOT NULL/旧路径收口。

回滚可在 contract 前切回旧读模型并停止 dual-write；不得删除新 append-only evidence。migration rollback 必须
先验证无新 writer 依赖，且不会丢失 subscription/ACK/DROP 证据。

## 16. 配置与环境隔离

标准 expectation 由版本化 manifest 生成，但 actual 必须由 daemon 解析最终 CLI 后注册；禁止用环境默认值
反推现场。activity policy 是受治理数据，不新增无消费者的 settings key。开发、WSL2 integration、
derivatives 模拟和所有 live profile 保持隔离。

## 17. 代码组织与依赖

预计改动范围：

- 新的 subscription catalog/domain 与 projection 模块；
- collector base/client ACK hook、两个 daemon/collector 注册与 typed event 写入；
- continuity/gap/bucket/reconciler；
- Batch B 显式 migration、ORM 和 schema guard；
- data-governance API、研究资格分类、可靠性检查与前端状态；
- 单元、WSL2/PostgreSQL integration、文档和 runbook。

历史 campaign、contract arithmetic/lineage、交易与参数应用不在本任务范围。

## 18. 审批项、Owner 与验收门

在实现前需要真人明确批准：

1. LF-B schema/migration 与 rollback window；
2. typed identity、activity model、各频道 SLA 和 policy owner；
3. legacy backfill 可映射范围及不可归因保留策略；
4. DROP+gap 采用同事务还是 obligation/reconciler；
5. simulated deployment/故障注入窗口、磁盘与数据库预算；
6. Data Owner、Risk Reviewer、Migration Owner 与 Rollback Owner；
7. API version、兼容矩阵、前端与 reliability consumer cutover 顺序及 rollback 判据。

只有以下全部满足才能关闭任务：所有新写入具备 subscription_id；三方 reconciliation 可解释；中间缺桶/
DROP/sequence gap 不能 complete；current lifecycle 不受 24 小时窗影响；300+ 规模下 stale scope、DROP
scope、DROP event、dropped rows 与分页分别真实；v1/v2 API、前端和 reliability 兼容/回滚契约通过；
PostgreSQL migration/rollback/并发/故障测试通过；目标 derivatives 模拟现场验证通过；独立复审无未关闭
P0/P1。本文状态在人工批准前保持 `BLOCKED_PENDING_HUMAN_LF_B_APPROVAL`。

## 19. 唯一待办

由真人召开并记录一次 LF-B 决策，统一批准或拒绝第 18 节七项内容，并实名指定 Data Owner、Risk
Reviewer、Migration Owner 与 Rollback Owner。在该决策完成前，不实施 schema、collector、API 或研究
资格改动；尤其不得用静态 inventory、特殊排除 liquidation symbol、统一 120 秒 SLA 或隐藏旧 95 告警
作为临时替代。
