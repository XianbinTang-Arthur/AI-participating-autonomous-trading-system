# FS-016 Runtime Readiness 可续租与单角色重启安全 P0 SOW

> 文档状态：现行实施任务书；本地候选已完成独立复审，最终 WSL2 集成、标准部署和完整故障矩阵仍 `OPEN`
> 最后核对：2026-08-29（核对基线 `main@f9bb249964364d457cd307f8ea427a65b7320888` + 当前 NATS exact-ownership 候选；以本文档所在最终 HEAD 为准）
> 核对范围：当前代码、测试、标准部署脚本、WSL2 derivatives 模拟栈只读现场
> 历史起点：[`fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md`](fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md)（其一次性 key 协议已被本文替代）
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 问题、现场证据与当前行为

四主进程在 `build_runtime()` 已注册 NATS durable consumer 后，只向 Redis 写一次
generation-scoped readiness key。该 key 的 TTL 为 300 秒，进程进入稳态后没有续租。标准
Compose 对主进程使用自动重启且同一部署内保持 generation 不变，因此系统存在确定性的恢复缺陷：

```text
部署成功并运行超过 300 秒
  -> market / decision / gateway / execution readiness key 全部自然过期
  -> 任一单角色因独立故障退出并由容器运行时重启
  -> 该角色重新 announce 自己，但三个健康 peer 不会重新 announce
  -> strict peer wait 60 秒超时
  -> 进程非零退出并再次重启，形成永久循环
```

2026-08-28 只读现场已观察到该链路：模拟栈部署基线为 `2de8da888ad1`，四主进程初次使用
同一 generation 完成 barrier；外部网络/DNS 抖动导致 execution 首次安全退出后，Redis 仅剩
execution 自己反复写入的 key，market/decision/gateway key 已过期。execution 随后持续产生
`runtime_ready_gate_timeout:execution:market,decision,gateway`，重启计数持续增长；其他三个主
进程仍为 healthy。首次退出与后续永久循环必须分开裁决：前者高置信与账户刷新成功进度超时及
交易所只读模式查询失败有关，后者由本任务描述的 readiness 生命周期缺陷直接造成。

原 FS-016 §7 同时规定“300 秒 TTL”“不是持续 liveness lease”和“同 generation 单容器重启可
复用 peer provisioning 事实”，三者在运行超过 300 秒后互相矛盾。本任务以现场证据修订该边界，
并替代原文 §7、§10 中的一次性 key 生命周期约定；generation 隔离、严格失败关闭和
INTEREST retention 安全目标保持不变。

## 2. 目标与非目标

### 2.1 目标

1. 使用与部署代次无关的全局 `aats:runtime:owner:<role>` key，从 key 空间上禁止旧/新 generation 同时占有一个 role；
2. 在任何 NATS 网络 I/O 前以 Redis `SET NX PX` 取得 protocol v2 `PROVISIONING` ownership，完成 runtime 装配后再以 compare-and-replace 原子升级为 `READY`；
3. generation 只保存在 owner payload 和 peer barrier 校验中；peer 只接受精确 generation/role、protocol v2、`READY` phase 与合法 instance payload；
4. 主进程存活且仍拥有该 role 时持续刷新 lease；新实例只能在旧 owner 安全删除或 TTL 到期后 claim，旧实例不得续租、覆盖或删除新值；
5. `PROVISIONING` 阶段的 NATS consumer callback 和 publish 都由显式投递门隔离；build 期 publish 只能进入 4,096 条/64 MiB 双上限内存缓冲，所有 peer `READY` 后必须先 flush，再开放 callback 和后台任务；
6. claim 成功时即启动不依赖父进程 asyncio、线程调度或 GIL 的独立 subprocess watchdog；lease 确定丢失、Redis 持续失败、event-loop/GIL 阻塞或 shutdown 卡住都必须在有界时间内硬退出；
7. Gateway 与 market/decision/execution 语义一致，且以单元、并发/故障、WSL2 模拟部署和真实单角色重启矩阵证明修复，不手工补 Redis key。

### 2.2 非目标

- 不弱化交易所 position mode、账户刷新、Kill Switch、reconciliation 或任何 live 门禁；
- 不改变 NATS stream、subject、durable name、retention、消息 schema 或交易业务 API；
- 不读取或修改 `.env.*`，不访问凭证，不发订单，不应用 recommendation/参数；
- 不启动 live profile，不执行真实资金操作，不 push；
- 不把本任务解释为完整 RDP、收益资格、历史数据 v2、Research OS G0 或生产上线验收；
- 不在本切片顺带重构 LF-B1.2、RDP UI 或数据连续性告警算法。

## 3. 设计与协议

### 3.1 Lease payload

每次进程实例在任何 NATS I/O 前生成不可复用 `instance_id`。owner payload 在该实例
的每个 phase 内 byte-stable，至少包含：

- `lease_protocol`：固定为 `2`；
- `process_role` 与 `generation`；
- `instance_id`；
- `phase`：只能为 `PROVISIONING` 或 `READY`；
- `announced_ts` 与 `pid`（仅诊断，不参与授权外推）。

key 固定为全局 `aats:runtime:owner:<role>`，不包含 generation。generation 仅参与 payload 身份和
peer barrier；因此旧/新部署不能通过不同 key 同时持有一个 role。instance ID 负责实例
fencing，二者不能互相替代。TTL 固定 60 秒，每 10 秒续租，失败关闭的安全停机
margin 为 30 秒，硬退出 grace 为 10 秒。claim/refresh 的本地 not-after 从请求发出前的
宿主 boot-time 时钟保守计算，响应延迟只能缩短窗口，不能放大 ownership；POSIX 使用
`CLOCK_BOOTTIME`，Windows 使用 `GetTickCount64`，两者都计入 suspend，和 Redis TTL 的继续流逝
保持同一保守语义。

### 3.2 原子 ownership 操作

`HotStateStore` 新增四个原子原语：

1. `set_if_absent(key, value, ttl_seconds) -> bool`：仅当 key 不存在时取得 role ownership；已有 owner
   存在时拒绝覆盖，等待其优雅删除或 TTL 过期；
2. `compare_refresh(key, expected_value, ttl_seconds) -> bool`：仅当当前原始值与本实例固定 payload
   完全相等时刷新 TTL；
3. `compare_replace(key, expected_value, new_value, ttl_seconds) -> bool`：只在原始 payload 精确一致时将本 owner 从 `PROVISIONING` CAS 为 `READY`；
4. `compare_delete(key, expected_value) -> bool`：仅当当前原始值仍属于本实例时删除。

Redis claim 使用单条 `SET NX PX`，replace/refresh/delete 使用单条 Lua 脚本完成比较与更改，
禁止客户端读后写；InMemory 原语只服务单进程/测试，strict 四进程 readiness 必须使用
Redis，不允许以进程内存冒充分布式 ownership。新实例
不得覆盖仍有效的同 role lease；取得新 lease 后，任何旧实例的 compare-refresh/delete 都必须返回
`False`，不能覆盖或删除新值。

### 3.3 生命周期与监督

```text
resolve final settings + connect Redis hot state
  -> SET NX PX claim global role as protocol v2 PROVISIONING
  -> start independent watchdog subprocess + PROVISIONING renewal
     parent/child clock: CLOCK_BOOTTIME or GetTickCount64
     parent identity: inherited pidfd or process creation FILETIME
     each successful write/refresh -> sliding hard fence <= write time + 50s
     claim -> READY absolute deadline = 180s (freeze at 170s + 10s fatal grace)
  -> 55s takeover quarantine (before any NATS I/O)
  -> build_runtime: connect NATS + provision durable consumers
     callback gate CLOSED; gated max_ack_pending=1
     build-time publish buffered (4096 / 64 MiB caps)
  -> CAS the same owner payload PROVISIONING -> READY
  -> wait exact-generation peers in protocol v2 READY
     lease task first fails -> ABORT gate / no callback or network publish
  -> flush bounded build-time publications
  -> atomically activate callbacks
  -> start business background tasks
  -> supervise OS stop + business critical failure + lease failure
  -> critical failure: freeze renewal immediately; sticky FATAL deadline; ABORT gate
  -> normal stop: freeze renewal; start 10s SHUTDOWN deadline
  -> stop business tasks/workers and drain/close NATS bus
  -> normal safe stop only: DISARM watchdog, then compare-delete unchanged owner
     safe stop point未到达 -> do not delete; retain TTL fencing
  -> stop container heartbeat last
```

Gateway lifespan 必须走同一 claim、两阶段 CAS、delivery gate、renew、deadline fencing 和
owner-aware cleanup；lease task 进入 runtime critical registry。`NatsDeliveryGate` 只有 sticky
`ABORT` 和有效 `READY` 语义：`ABORT` 唤醒已阻塞 callback 但不 parse/persist/handler/ack/nak，
也拒绝后续 publish；安全激活必须先 flush 预激活缓冲，再开门。HybridEventBus 必须在
critical/observer 两路都尝试关闭后汇总并向上抛出固定、脱敏的失败；ApplicationRuntime
据此禁止 release hook，不能把底层 NATS drain 失败吞掉后删除 owner。

watchdog subprocess 从 claim 成功即存在，先固定父进程身份再返回 ready handshake：POSIX 由父进程
预开并继承 pidfd，Windows 打开 process handle 并校验父进程的 creation FILETIME，
避免 PID 重用误杀。父进程与 child 都用同一类 boot-time 时钟。每次成功写入或刷新
`PROVISIONING` lease 后，本地 hard fence 最多滑动到该次写入时刻后 50 秒；这不是 claim 后
50 秒的总上界。claim 到 `READY` promotion 另有 180 秒绝对上界，续租只能延长 Redis TTL、绝不能
延长该上界；第 170 秒冻结续租并进入最后 10 秒 fatal grace。`READY` 续租用 30 秒 safety margin
触发安全停机，再给 10 秒有界 grace 完成清理。
compare-refresh/replace 明确返回 `False` 意味着 ownership 已确定丢失，必须零宽限进入立即硬退出，
不允许“再试一次”。fatal watchdog 一旦置位便不可 disarm，即使 coroutine/finally 返回也
保持到进程真正退出；正常 `SHUTDOWN` 状态拒绝后续 rearm，但允许在安全 cleanup 完成后
`DISARM`。watchdog 控制帧有固定 protocol/nonce/sequence/长度上限，晚于旧 deadline 到达的
REARM/DISARM 不得延长生命；deadline 路径只做进程级硬退出，不执行日志或业务 I/O。

新实例即使在 Redis key 因重启、人工删除或历史淘汰策略而提前消失后取得 claim，也必须先保持
`PROVISIONING` 续租并静默隔离 55 秒；该 quarantine 覆盖旧 protocol-v2 实例从最后一次成功写
lease 到 `PROVISIONING` hard fence 的 50 秒保守上界，再留 5 秒调度余量。它不替代 protocol
v1 -> v2 的 full-down，也不授权 rolling upgrade。

### 3.4 NATS consumer、连接与 Redis 容量

- 仅 strict 四主进程 NATS/hybrid 注入 `NatsDeliveryGate`，其 push subscription 的 broker 预取窗口固定为
  `max_ack_pending=1`。nats-py callback 串行执行；门关闭时只有队首消息能发送 progress ACK，
  若继续预取 256 条会让客户端队列中其余消息在隔离期耗尽 `max_deliver`；因此不能只依赖队首 WPI。
  non-strict、in-memory 和 monolith 路径不注入 gate，也不强制 `max_ack_pending=1`。
- 已有 durable 的可变项只能按声明边界迁移并再次 `consumer_info` 回读：`max_ack_pending` 收敛到
  `1`；只有 snapshot/transient 的正数旧 `ack_wait` 可向声明目标增加；event 的 `ack_wait` drift 与
  任意 `max_deliver` drift 均失败关闭。对 `DeliverPolicy.ALL` 的关键事件 durable，安全投影内的
  consumer 行为配置 drift 一律失败关闭，禁止删除重建导致 cursor 重置。flow control/idle heartbeat
  历史差异当前保留并告警，不在 qualification/supervisor 投影内，仍是运行验收边界。LAST/NEW
  snapshot/transient durable
  只在运行时同时满足 strict delivery-gated、policy 非 ALL，并且（outstanding 超过目标，或窗口正在
  收缩且 outstanding 非零）时按既有语义
  重建：运行时会在 bind 前明确 delete/recreate，LAST 只从当前最新快照重新开始，NEW 只接收重建
  后的新事件，旧 pending/cursor 按其声明语义丢弃；自动重启也可能进入该分支。运行时代码不接收
  或校验“已 full-down”信号；标准 full-down 是首次 v1 -> v2 切换的额外发布门禁，不是此分支的
  代码内边界。该分支不适用于任何 ALL 金融事件 cursor。
- runtime 在处理现存 durable 前冻结 broker `created`、实际 push `deliver_subject` 与 delivered/ack-floor
  四维 cursor；update 回读、post-bind、READY 前和稳态 supervisor 都要求同一 created/inbox、durable
  identity 与配置，并拒绝任一 cursor 回退。这关闭了 pre-check 到 update/bind 之间的同名重建竞态。
  post-bind 校验失败只允许先 ABORT gate、再有界取消本地 subscription；不得 drain、ACK、删除或重建
  broker durable，也不得把实际 inbox 写入日志/证据。
- 首次 v1 -> v2 ACK-window cutover 必须先停止相关生产者，再于 app up/bind 前取得 exact ownership
  只读证据。ownership manifest 是 `aats/bus/consumer_ownership.py` 中人工维护的 authoritative
  declaration，动态 assembly 测试会实际装配四个 `build_runtime()` 并证明两者精确一致；共 `77` 个
  stream-backed durable：gateway `31`、market `8`、decision `27`、execution `11`；event `49`、
  snapshot `24`、transient `4`。persist-only `system.audit_records` 没有 JetStream consumer，明确排除。
  标准 deploy 的应用 stop
  只证明生产者已停止，**不证明 JetStream 已 drain**。现行入口会在只接受 app `exited/dead` 或明确
  not-found 后，受控仅启动基础设施/NATS，以规定 `~/aats-venv` 从 loopback 分页枚举全部
  stream/consumer，并核对分页/总数、每个 stream 的 consumer 数、stream/durable/role/topic/semantics、
  created、四维 cursor、safety-projection immutable/mutable config、窗口与 outstanding；实际 inbox
  仅记录存在性。`existing_container_preserved` 下声明项缺失或实际 consumer 未归属都失败关闭；
  `proven_fresh_install` 的 preflight 要求 consumer 集合为空，app-up 后最终证据才要求 exact `77`。
  不能以总数相同替代集合与配置精确一致。
  旧 `max_ack_pending > 1` 时存在任意未 ACK，或配置虽已
  为 `1` 但 broker 仍携带 `num_ack_pending > 1` 的历史交付，均以固定码
  `nats_critical_consumer_ack_window_migration_requires_drain:<durable>` 失败关闭，不执行 update/bind，
  绝不 delete/recreate、reset cursor、purge 或人工 ACK 绕过。窗口已为 `1` 且只有一个合法 in-flight
  属稳态，不应被误判为首次迁移。
- NATS 可保持底层无限重连，但断连连续 30 秒或 client 永久关闭会设置 sticky terminal failure；
  watcher 作为 runtime critical task 上浮到 daemon/Gateway，触发冻结续租、ABORT 与有界退出。
- 已绑定 critical durable 由独立 consumer supervisor 默认每 5 秒只读核验。明确 `NotFound`、同名
  重建、push inbox 变化、任一 cursor 回退，或安全投影内的 ack/deliver/filter/replay/headers/pause/backoff/
  rate-limit/inactive-threshold/mem-storage/start-position/ack_wait/max_ack_pending/max_deliver 配置漂移
  立即 terminal；management 查询、
  push binding 或 idle-heartbeat 持续失败达到 30 秒有界 terminal；gate 已激活且存在 backlog 时，
  delivered/ack-floor/pending 签名在 `max(30 秒, 2 x ack_wait)` 内无进展也 terminal。该监督不能由
  core TCP connected 或容器 healthy 替代。
- Compose Redis 使用 `noeviction`。readiness owner 与热缓存当前共用 Redis；达到 384 MiB 上限时
  写入必须显式失败并由上层失败关闭，不能以淘汰带 TTL owner key 换取表面可用。

### 3.5 部署时序与残余安全边界

标准 deploy 在 profile/live gate 与 profile 解析后、任何 mutation 前取得长寿命 WSL `flock`。
生产路径固定为 `/tmp/aats-standard-deploy.lock`；`AATS_DEPLOY_LOCK_FILE` 只有在
`AATS_DEPLOY_TEST_MODE=true` 的隔离测试中才可覆盖，标准部署尝试覆盖会非零退出，不能通过多个
路径拆分全局互斥。单一 WSL holder 持有同一个 fd；Windows heartbeat 每 3 秒刷新独立 lease，
holder 连续 12 秒未见刷新才释放 fd，且不删除 lock inode。新 holder 即使已取得 flock，也会扫描
其他 `/tmp/aats-standard-deploy-lease-*` 和同一锁作用域的 durable active marker；任一 predecessor
lease 仍不超过 12 秒，或任一 active marker 仍存在时，新 holder 持续刷新自己的 lease 并隔离等待，
直至两类前任证据均清除才报告 `ACQUIRED`。active marker 不使用 12 秒 TTL；它是无法证明远端 mutation
已经结束时的持久阻断证据，不能靠时间自动失效。

锁贯穿预检、提交/同步、generation、构建、停启、迁移、健康检查、最终证据与报告。每个外部步骤
spawn 前先验证 holder PID、heartbeat PID、flock 和当前作用域不存在未闭合 marker，拒绝嵌套
supervised child；spawn 后立即将 child PID/context 写入全局 active 登记。最终 launch 前的 cancel
handshake 可以证明命令从未获得 mutation 能力；launch 后，失锁、`TERM`、`HUP`、`INT` 或普通
`EXIT` 都不能通过杀死 Windows/`wsl.exe` 客户端推断 Docker daemon/WSL 端 mutation 已停止，而是
保持 lease/flock 并等待同步 guard 取得实际完成证明。父进程或 guard 硬丢失时，active marker 留存，
keeper 与后继 holder 都被阻断，直至按精确证据恢复；锁不会在旧 mutation 仍可能运行时释放。

完成证明分为三类：已知本地同步进程返回后可清理自身 marker；未分类 transport 仅在返回零时可清理，
非零视为歧义；WSL/root 命令由 WSL-side wrapper 在真实远端命令返回后原子写入七字段 completion ACK，
绑定 marker、远端语义退出码、I/O 模式、stdout/stderr 字节数及 SHA-256。默认 `capture` 同时在本地以
`0600` 暂存两路输出，只有 ACK 与本地字节数/摘要完全一致时才回放并返回远端语义状态；`stream` 仅用于
需实时输出的构建，`quiet` 用于明确静默步骤。ACK 在 proof 跨越 WSL transport 前保留，允许“marker 已
删除但响应丢失”的幂等重读；初次 client 非零、ACK 缺失/畸形、摘要不一致、回放或清理失败均返回
16 并保留阻断证据。释放阶段在 NATS 快照清理后再次扫描 marker 并复核 holder；若原状态待成功则改为
16，已有非零状态保持不变。标准代码同步已合并为单个 ACK 支持的 WSL Git 事务，dirty checkout、
分支不匹配或同步后 HEAD 不一致等远端语义状态可准确传播并清理自身 marker，transport 歧义仍保留。
当前 Windows Git Bash -> WSL 协议只有聚焦 smoke 证据，不等于标准部署 PASS。

停止阶段对所有已知 profile 的七个应用容器执行 15 秒有界 stop；逐个只接受 `exited/dead` 或明确
not-found，其他状态与 inspect 失败全部停止发布。quiescence 基线记录容器 ID、状态、`StartedAt`、
`FinishedAt`、`RestartCount`。随后受控确保仅基础设施在线，在 full-down 前执行第一次 preflight；
PASS 后才以 5 秒 down 关闭 Redis/NATS/Postgres，并为 full-down 后的容器状态建立新基线。正常
infra/schema 完成后、app up 前执行第二次同等 preflight。strict v2 增加 55 秒 quarantine，应用 health
默认预算相应调整为 210 秒；这是等待合法安全启动的预算，不是放宽健康门。

每次 preflight 都在查询 NATS 前后重读七容器指纹，并查询这两个快照所界定的精确时间窗内的 Docker
lifecycle events；任一指纹变化、相关 event、paused/restarting/removing/unknown、inspect 错误、NATS
查询失败或 BLOCKED 都失败关闭并保持 NATS/Redis/Postgres 在线。首次安装或 NATS 原先未运行时由
基础设施-only up 提供查询面，不能把不可达当 PASS。preflight 不得为制造零值而 ACK、删除/重建
consumer、update、reset cursor 或 purge stream。outstanding-only 阻断只能在人工批准的变更窗保留
旧状态、使用匹配旧版本消费者自然 drain 至零后重跑；immutable drift 必须人工 release review。
PASS JSON schema v3 写入 `artifacts/deployments/`，绑定 deployment lock id、generation、deployed
commit、quiescence、ownership/query projection，以及适用于该 bootstrap mode 的声明与未归属/缺失
集合。writer 重新执行共享
投影验证并从原始 pre/post rows 独立重算 continuity，不信任 artifact 自报摘要。最终 deployment
evidence 同时验证 pre/post 两次 schema v3 artifact 的同值、只读、完整查询、quiescence、chain、相对路径与 SHA-256；
它还封存两次最终 no-secret canonical durable projection 及各自 SHA-256，覆盖 exact identity、created、
四维 cursor、immutable/mutable config、窗口、outstanding、status/blockers，使审计者可独立重算
post-to-final 与 final double-read 连续性。

NATS 的八个非秘密容量目标不再由多个阶段重复读取可变 profile。标准入口在同步代码并取得 deployed
commit 后，只用严格 allowlist 解析 profile 一次，将全部八键补齐、规范化并编码为确定性快照；root
在 `/run/aats-deploy/` 的受管目录内原子写入 `root:root`、`0444` 的本次锁 token 专属文件。preflight
只读该快照，所有模拟 profile 的必需应用容器均把它作为最后一个 `env_file`，并携带同一 target
manifest SHA-256 label。健康边界与最终 writer 都要求每个容器 label 精确匹配 preflight manifest；
app-up 前后及证据生成前再次验证快照的类型、owner、mode、严格八键内容与摘要。正常退出仅尝试删除
本次精确路径；清理失败会告警，并可能留下不含凭证的 stale file。NATS 容器共享身份投影还同时校验
Compose project/service、唯一 RW 标准
本地 volume、固定 `Config.Image` digest ref，并将容器实际 image ID 与该 pin 当前解析出的 image ID
绑定，不能以同名、同 label 的其他镜像冒充。

应用健康通过后，标准入口执行 40 秒、每 2 秒一次的主动稳定观察；每次同时要求 running、healthy、
`FailingStreak=0` 与最新 healthcheck `ExitCode=0`。最终证据仅接受自健康边界起 35–60 秒的逻辑窗，
并逐项扫描 Docker 当前保留的 health log；60 秒上限低于五条日志乘以最短 15 秒 health interval，
避免边界后的单次失败被后续成功挤出。Docker Engine event monitor 从本地 socket 建立实时分段观察，
在逻辑 cutoff 后封口并再次采样全部应用、NATS、volume 与 base image；derivatives collectors 还必须
各有且只有一次位于 start 后、健康边界前的 `archive-path` 读取动作。该证据明确保持
`BOUNDED_OBSERVED / PASSED_WITH_TRUST_BOUNDARY`：Moby event broker 不是有序无损审计源，daemon/client
时钟未证明对齐，exec、network attachment 与跨容器 volume 访问不可见，不能升格为完整审计。

上述代码、聚焦测试与隔离 smoke 已落地。2026-08-29 03:43Z 的标准 derivatives 尝试使用已提交
`f9bb2499` 的旧 schema v2 checker：停净七个 app 并保持基础设施在线后，第一次 `pre_full_down`
扫描 `78` 个 consumer，只识别 `49` 个 event durable，把 `28` 个合法 snapshot/transient 与一个
`aats-codex_manual_resume-system_operator_command_responses` 一并列为 `29` 个 unexpected 后安全阻断。
该 artifact 证明旧 v2 ownership 模型过窄，不能证明新 v3 的 exact `77+1` 资格。当前候选随后对同一
broker 做过非发布只读诊断，得到声明 `77` + 未归属 `1`，但仍须提交后用标准入口生成 v3 artifact。系统
没有进入 full-down/app-up，七个 app 保持停止，未知 durable 未被修改。该项必须由真人 owner/release
review 决定，不能自动删除；在此之前标准部署、完整 Docker 故障矩阵仍 `OPEN`。

本协议仍不是下游 Postgres/NATS/交易所执行 sink 强制校验的单调 fencing token。若 Redis owner
truth 丢失/被错误恢复与独立 watchdog 或 OS termination 同时失效，55 秒 quarantine 只能提供
保守时间隔离，不能形式化证明旧实例与新实例绝不重叠产生副作用。真实资金重新开放前，必须补充
下游 fencing token 或取得等价的受控旧实例死亡证明，并完成双故障矩阵。此前保持
**REAL-MONEY PRODUCTION: NO-GO**。

### 3.6 失败与脱敏

- announce、strict peer poll、lease refresh 的底层异常只向外转换为固定 RuntimeError code；
- 日志记录 role、generation、固定 error type 和 ownership result，不记录 Redis URL、底层异常正文或 payload；
- 单次瞬态 refresh 异常可在当前 lease 的安全重试窗口内重试；进入到期前 30 秒安全停机窗口仍未成功即失败关闭；
- compare-refresh/replace 返回 false 视为 ownership 已确定丢失，零宽限硬退出，不能用普通 set 抢回；
- compare-delete 返回 false 只表示本实例已不是 owner，必须保留新实例 lease，不能覆盖原始退出原因。
- 任何启动/运行失败先将 delivery gate 置为 `ABORT`；未 flush 缓冲、未投递 callback 和未启动 background 不得被记录为 ready。
- 任何关键失败先设置 lease stop event，冻结后续续租，再收紧 fatal deadline；正常停机也先冻结续租，不能在 cleanup 卡住时继续维持 `READY`。

## 4. 兼容性、性能与容量

- HTTP、数据库、事件 envelope 与前端契约不变；Redis readiness 是 protocol v2 且与 v1 不兼容；
- monolith/in-memory 或无 peer 的非严格路径保持兼容，不注入 delivery gate、不强制
  `max_ack_pending=1`；strict 分布式路径仅允许 Redis ownership；
- 每个主进程稳态只增加一个低频 Redis 原子 refresh；四角色总 QPS 保持常数级；
- 全局最多四个当前 role owner key，旧 generation payload 不会满足新 barrier；安全停机删除，非安全停机留给 TTL 回收；
- 每个 strict 主进程新增一个只导入 Python 标准库的独立 watchdog subprocess；不新增数据库连接、第三方依赖或长期文件；
- build 期 NATS publish 可占用最多 64 MiB 且最多 4,096 条，任一上限先到即失败关闭，不允许无界内存增长。
- gated consumer 将每个 subscription 的 broker prefetch 固定为 1；当前 callback 本就串行，不减少其实际 handler 并行度，但真实 NATS 吞吐与 backlog 行为仍需运行验收。

## 5. 测试与验收矩阵

### 5.1 单元与协议测试

1. InMemory/Redis store 的 set-if-absent、compare-replace、compare-refresh、compare-delete 成功、missing、mismatch、expired 语义；
2. 全局 role key、protocol v2 payload、`PROVISIONING -> READY` CAS、generation/role/instance 与 TTL；
3. 时间压缩单测证明运行超过初始 TTL 后 lease 仍刷新；生产运行验收必须真实跨越 60 秒 TTL 与多个 renewal 周期；
4. dead/never-announced peer、`PROVISIONING` peer、错误 role/generation/protocol、畸形 payload 继续失败关闭；
5. Redis refresh exception、lease 到期、ownership 替换触发固定失败且不泄露异常正文；
6. 旧实例 refresh/delete 与新实例 claim 竞争时，新实例值不被覆盖或删除；
7. claim、PROVISIONING 续租、watchdog READY handshake 与 55 秒 quarantine 在任何 NATS I/O 前完成；父/child clock 一致且计入 suspend，pidfd/creation FILETIME 防 PID 重用；event-loop/GIL 阻塞不能越过每次成功 PROVISIONING 写/续租后的 50 秒滑动 hard fence，也不能越过 claim→READY 180 秒绝对上界（170 秒冻结续租 + 10 秒 fatal grace）；
8. delivery gate 在未激活时不 parse/persist/handler/ack/nak，ABORT 竞态优先；4,096 条/64 MiB 双上限、flush-before-callback 和 flush 失败全部失败关闭；gated consumer 使用 `max_ack_pending=1`；已有 critical ALL durable 仅在 `num_ack_pending=0` 时从旧窗口原位降到 `1` 且 cursor 不删除；旧窗口仍有未 ACK 以及窗口已为 `1` 但 outstanding 大于 `1` 均固定失败关闭，且不调用 update/delete/subscribe；仅 snapshot/transient 的正数 `ack_wait` 可向声明目标提高，event `ack_wait` 与任意 `max_deliver` drift 阻断；LAST/NEW 的 ACK-window backlog 重建仅在 strict delivery-gated、policy 非 ALL，并且（outstanding 超过目标，或窗口正在收缩且 outstanding 非零）时发生，safety-projection immutable drift 另有声明丢弃语义重建分支；两类都分别验证“仅最新/仅未来”的真实语义；自动重启也可进入运行时分支，标准 full-down 是额外发布门禁而非运行时信号；
9. lease 在 peer wait/flush/background start 前失败时操作被取消；确定失租零宽限 hard exit，fatal watchdog 不可被 finally disarm；
10. Gateway barrier/lease failure 不设置伪 ready，cleanup 不删除新 owner；
11. 关键故障立即冻结续租且 fatal 不可解除；正常 stop 先冻结续租、进入 10 秒 SHUTDOWN deadline、停止业务并 drain NATS、disarm 后再 owner-aware delete；非安全停机不删除 owner，由 TTL fencing；
12. NATS 连续断连 30 秒和永久 close 进入 critical failure；critical durable `NotFound`、created/inbox/name/config drift 或四维 cursor 回退立即 terminal，management/push/heartbeat 持续失败和 backlog 无进展有界 terminal；update/bind 前冻结 identity，回读/post-bind/READY/steady state 关闭同名重建竞态；post-bind 失败无 drain/ACK/delete 副作用；Redis `noeviction` 与写失败上浮；
13. 原 FS-016 generation、deploy injection、210 秒 health budget、monolith 与 optional 路径回归，并明确拒绝 protocol v1/mixed-version peer；标准 deploy 使用不可由生产覆盖的固定全局 WSL `flock`，任一 fresh predecessor lease 或同作用域 durable active marker 触发 takeover quarantine；每个外部步骤 spawn 前 fencing、spawn 后唯一 active child 全局登记，launch 前取消不执行 mutation，launch 后丢锁或 `TERM/HUP/INT/EXIT` 保持 lease/flock 并等待 guard 的真实完成证明；本地/未分类/WSL ACK 三类完成语义、七字段输出完整性、远端语义状态传播、proof 幂等重读、release 状态 16 与 poison marker 均须回归。七容器 quiescence 以 ID/status/StartedAt/FinishedAt/RestartCount 和精确 Docker events 区间验证；full-down 前与 app-up 前各执行一次只读全量分页 preflight；失败保留 NATS；两次 schema v3 artifact 都必须与最终证据的 lock id/generation/deployed commit/quiescence/path/hash 一致，并重算原始 pre/post continuity。
14. NATS 八键 target snapshot 必须规范、确定、无非白名单值、root-owned 只读且绑定 lock token；preflight、Compose 末位 `env_file`、所有必需容器 manifest label、健康边界和最终 writer 必须同摘要。NATS post-recreate/final 身份必须同时匹配固定 `Config.Image` pin 与该 pin 的实际 image ID。主动稳定观察必须拒绝 healthy 但 `FailingStreak>0` 或最新 `ExitCode!=0`；最终逻辑窗不得超过 60 秒，防止五条 Moby health log 淘汰边界后失败。
15. 人工 authoritative ownership manifest 必须经动态 assembly 测试证明精确匹配 gateway `31`、market `8`、decision `27`、execution `11`，共 `77` 个 durable 与 event/snapshot/transient `49/24/4`；preserved install 的 query 总数、逐 stream consumer count、声明集合与实际集合精确一致，missing/unowned/错 stream/config 均失败关闭；fresh install preflight 必须为空且 app-up 后 final 必须 exact `77`；最终证据两次 canonical durable projection 的 SHA-256 对任一同数量 identity 或 config 替换都必须变化，且不得包含实际 inbox、凭证或连接信息。

### 5.2 模拟运行验收

代码提交且静态验证通过后，只能使用仓库标准 derivatives 模拟部署入口。验收至少包括：

- Windows、WSL checkout、容器 evidence commit 与 readiness generation 对齐；
- 四主进程、RDP daemon、Postgres、Redis、NATS 和 collectors 的组件健康；
- 标准入口使用 210 秒应用健康预算；任何 mutation 前取得固定 `/tmp/aats-standard-deploy.lock` 的
  长寿命 WSL `flock` 并持有至最终报告，拒绝生产 lock-path override，同时等待 fresh predecessor
  lease 与同作用域所有 durable active marker；每个外部步骤 spawn 前围栏、spawn 后登记 active child，
  launch 前取消不执行 mutation，launch 后信号/退出/失锁保持 lease/flock 并等待实际 completion proof；
  WSL `capture` 的七字段 ACK、输出摘要验证后回放、远端语义状态、proof 幂等重读及所有歧义失败关闭均
  取得运行证据；标准代码同步作为单个 ACK 支持的 Git 事务完成，语义拒绝不毒化全局锁；
  stop 七个 app 时只接受 `exited/dead` 或明确 not-found；以 ID/status/StartedAt/FinishedAt/RestartCount
  和精确 Docker events 区间验证 quiescence；基础设施-only up 后、full-down 前取得第一次 loopback
  全量分页只读 preflight PASS，full-down 后建立新基线，infra/schema 完成后、app up 前取得第二次；
  最终证据验证两次 v3 PASS 的同 lock id/generation/deployed commit/quiescence、chain 及 path/hash，
  重新计算 pre/post continuity，并封存两次最终 canonical durable projection/hash；
- 四个 strict role 都在任何 NATS I/O 前启动 PROVISIONING 续租与独立 watchdog，并实际完成 55 秒 quarantine；日志/受控探针证明父子 boot-time clock 和稳定父进程身份围栏生效；
- 等待超过 lease TTL 后，按受控方式逐个重启四主角色并观察重新加入；
- restart count 收敛，日志不再出现 readiness timeout 永久循环；
- wrong generation、Redis claim/replace/poll/refresh、容量写失败和 NATS 连续断连超过 30 秒的故障注入保持失败关闭且无 publisher/伪健康；
- preserved install 的首次 cutover 在生产者停止后、app up 前保存 exact `77` durable ownership 与逐 consumer 的只读 `consumer_info` 证据；missing/unowned 均阻断。fresh install preflight 则必须为空，并在 app-up 后 final 证明 exact `77`。event 在旧窗口下必须 `num_ack_pending=0` 后才把窗口原位更新为 `1`，snapshot/transient 只执行声明语义允许的 reconcile/rebuild；标准 stop 不冒充 drain，durable identity/created/inbox/cursor 连续；隔离期间不存在 broker 预取消息耗尽 `max_deliver`；
- 注入 consumer delete、配置漂移、management 查询故障、push unbound、heartbeat inactive 与 backlog stall；逐项证明立即或有界 terminal、gate ABORT、进程退出及告警送达，不能以 core TCP 或容器 healthy 掩盖；
- 关键失败立即停止 renewal；正常停止在 10 秒内完成业务/NATS drain、watchdog disarm 与 owner-aware delete，超时则硬退出并保留 TTL fencing；
- 首次 v1 -> v2 发布和任何回滚都采用标准 full-down，确认 gateway/market/decision/execution 旧进程全部停止后再 app up；不做 rolling 或 mixed-version 验收；
- Redis owner truth + watchdog/OS termination 双故障和下游 fencing 若没有获准、可重复的测试入口，必须保持明确 `OPEN`，不能把单故障通过外推为排他证明；
- Kill Switch、reconciliation 和模拟环境 NO-GO 状态不被放宽；
- 无 live profile、真实订单、真实资金或参数应用副作用。

受控单角色重启/故障注入若仓库尚无标准入口，本轮先补最小测试入口或保留明确 OPEN，禁止用手工
`docker compose` 绕过部署纪律。

## 6. 代码与文档范围

预计修改：

- `aats/storage/hot_state_store.py`；
- `aats/bootstrap/process_lifecycle.py` 与 stdlib-only `aats/bootstrap/readiness_watchdog.py`；
- `aats/bootstrap/config.py` 与 `aats/bus/nats_bus.py` 的安全停机结果传播；
- `apps/api_gateway/main.py`；
- `deploy/wsl2-dev/docker-compose.yml`、`scripts/deploy.sh` 与对应部署契约测试；
- readiness、HotStateStore、Gateway 与 lifecycle 相关单元/集成测试；
- `CLAUDE.md`、`deploy/wsl2-dev/README.md`、`docs/README.md`、`docs/task/README.md` 和本任务状态。

不修改数据库 migration、交易配置、profile、推荐参数、Research OS 独立仓库或历史 campaign manifest。

## 7. 回滚与停止线

- 静态阶段可按本任务范围回退 lease protocol、store 原语、测试与文档；
- protocol v1 -> v2 首次发布以及回滚必须通过标准 derivatives 模拟部署入口 full-down/full-up，并在新应用启动前确认 gateway/market/decision/execution 旧进程全部停止；
- 禁止 rolling upgrade、mixed-version 运行、单角色跨协议替换或手工删/写 owner key；标准入口不能证明已停净时必须停止发布；
- 任一原子 fencing、失败关闭、Gateway 一致性或独立复审存在 P0/P1 时停止部署；
- 任一测试失败、工作树混入无法归属的用户改动、标准部署入口失败或现场出现 live 副作用时立即停止；
- 原一次性 300 秒 key 实现不能作为安全回滚目标；必要时保持模拟栈 HALTED 并记录阻断。

## 8. 完成定义与当前状态

完成必须同时满足：代码实现、聚焦与全量单测、必要 WSL2 集成、独立对抗复审、标准模拟部署、单角色
重启矩阵和文档同步全部通过；Windows/WSL/容器基线一致；没有未关闭 P0/P1；没有 live 副作用。

当前状态：**P0 根因已取得静态、可复现与历史运行时证据；protocol v2、两阶段
ownership、claim 即续租、55 秒 quarantine、delivery gate、有界 build publish 缓冲、strict gated
`max_ack_pending=1`、durable 原位更新与首次 cutover drain gate、critical consumer runtime
supervision、30 秒断连监督、Redis `noeviction`、210 秒部署健康预算与独立 subprocess watchdog 属
本地候选实现。父子 boot-time clock、pidfd/creation FILETIME、关键故障冻结续租、每次成功
PROVISIONING 写/续租后的 50 秒滑动 hard fence、claim→READY 180 秒绝对上界（170 秒冻结 + 10 秒
fatal grace）和正常 cleanup 10 秒上界也已进入候选代码。non-strict、in-memory 与 monolith 不注入
gate、不强制 `max_ack_pending=1`。标准入口另已加入固定生产路径的长寿命 WSL `flock`、仅测试可用
的 lock-path override、fresh predecessor lease 与 durable marker 双重 takeover quarantine、外部步骤
spawn fencing/active child 全局登记、本地/未分类/WSL ACK 三类 completion 语义、七字段输出完整性、
幂等 proof、release 失败关闭、单事务代码同步、七容器 lifecycle quiescence 证据、full-down 前和
app-up 前两次只读 preflight，以及绑定 lock id/generation/deployed commit/quiescence/path/hash 的最终
证据校验。只有 guard 已取得对应完成证明，或 wrapper 仍处在 final launch 前取消窗口时，才可对自身
active marker 执行 10 次、每次间隔 200ms 的跨 WSL 删除与 absence 证明；父 trap 只能验证 marker 已
消失，不能擦除硬崩溃留下的歧义 poison marker。应用健康边界后，应用和 NATS 均进入
40 秒、每 2 秒一次的主动稳定观察；NATS
`Health.Log` 与稳定 identity fingerprint 分离，但 writer 会在三次采样中绑定同一 container ID，
拒绝边界后任一非零检查并要求至少一次边界后成功，因而“失败一次后恢复”不能被写成成功证据。
Windows Git Bash -> WSL 锁协议只有聚焦 smoke 证据，不等于真实标准部署通过。**

真实 NATS 集成 `test_deleted_critical_durable_is_terminal_while_core_stays_connected` 已证明 core
TCP 仍连接时删除 critical durable 会 ABORT gate 并触发 terminal；首次窗口收缩且 outstanding
未清零的真 NATS 集成也已证明固定失败关闭并保留 durable identity/created/cursor；聚焦真 NATS
验证还覆盖 LAST 与 NEW 两类重建后的声明丢弃语义。后者只证明运行时 strict gate/policy/outstanding
条件下的重建结果，不证明 full-down 是代码内信号，也不覆盖自动重启或标准发布端到端时序。这些
证据不覆盖真实标准部署中的两次只读 drain/quiescence preflight，也不覆盖真实服务容器退出、网络/
push/heartbeat/backlog stall、告警或恢复时序。

最后一次 NATS 健康窗、WSL completion ACK、输出完整性、幂等 proof、marker cleanup、durable marker
scan、单事务代码同步、exact ownership、runtime TOCTOU 与最终 canonical durable evidence 修复后，
当前候选的 NATS/部署门禁聚焦合集 `351 passed`，全量
`6531 passed / 31 skipped / 259 subtests passed` 已于 2026-08-29 通过；Ruff 已对本切片全部变更 Python 文件复跑通过，
此前真实 Git Bash 语法检查和六项隔离 smoke 通过。隔离 smoke 不等同于真实 Docker/WSL 发布，SQLite
deprecation warnings 也不属于本切片安全通过条件。两路独立代码复审未留下 P0/P1；Docker event delivery loss、
daemon clock 未校准、HTTP ready 早于订阅确认及 exec/跨容器 volume blind spot 仍作为不完整信任边界
显式披露。真 Redis 集成、标准 full-down 模拟发布、完整真实 NATS/Docker
启动-断连-consumer supervision-逐角色重启、双故障与下游 fencing 矩阵仍保持 `OPEN`，不得声称新候选已完成运行验收。此前 execution 重启循环和 Windows/容器基线
不一致是 2026-08-28 已记录的历史现场证据，不证明当前时刻仍相同。2026-08-29 的标准尝试只证明
基础设施在线与第一次 preflight 安全阻断；七个 app 当前有意保持停止，故整体模拟栈不是 healthy，
也不得宣称部署通过。仓库当前没有获准的单角色重启入口，因此
真实逐角色重启矩阵保持 **OPEN**，不能用手工 Compose 或容器重启冒充验收。真实资金继续
**NO-GO**。
