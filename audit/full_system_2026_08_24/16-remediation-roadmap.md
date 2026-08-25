# 16 修复路线图（未实施）

本文件只定义建议顺序和验收证据，不授权修改代码、部署或 live 操作。每一阶段都应新建设计/实施任务，经人工批准后执行。

> Phase 2 更新：`FS-002` 已升为 P0，必须先于其余项目关闭；当前 GO/NO-GO 验收入口以 [20-go-no-go-gates.md](20-go-no-go-gates.md) 为准。`FS-004/005/008` 虽降为 P2，仍保留在路线图中作为条件门禁/验证任务。

> Phase 3D 状态边界：标题“未实施”是 Phase 1/2 路线图快照。当前工作区已实现
> FS-006 的 task-exit supervisor 第一层：critical exception/cancel/提前返回会使
> daemon 非零退出，FastAPI health 会 `503`。本节要求的 last_success、lag、
> dependency、hang 注入、真实容器与 readiness packet 仍未实施，详见 `24`。

> Phase 3K 状态边界：账户刷新、执行同步、对账、execution outbox/command、
> Phase 1 shadow 与 trial guard 七条固定周期关键循环已有成功进度 deadline；永久
> await 或连续无成功周期会分类为 `stalled` 并触发非零/503。事件驱动任务的
> connection/freshness/queue-lag、整体 event-loop 外部监督和目标运行验证仍未完成，
> 详见 `31`。

> Phase 3L 状态边界：FS-002 已把长期恢复 state 与在线增险 permission 分离；
> Gateway/monolith 维护同 generation 的 15 秒 Redis lease，execution 最终 fence
> 只读且不能续租。全分区的“无限期旧 RUNNING 权限”已收紧为代码 TTL 上界，但
> 真实 Redis/NATS 四进程单向分区、服务器 TTL、crash/restart、告警、多实例和独立
> 复核仍未实施，详见 `32`。

> Phase 3M 状态边界：FS-001 的 profile apply 与 rollback 已全部改为授权后无写入
> `501`，历史 Saga 不再由 route 调用；错误成功入口已收敛。execution-owned
> activation、generation、worker ack/readback、对称 reverse saga 与历史 applied 数据
> 对账仍未实现，详见 `22`、`33`。

> Phase 3N 状态边界：FS-014 已用 `ohlcv_participation_cap_v2` 收敛三类订单的
> 无量全成、bar-volume 容量、partial fill、bounded taker 成本与 fixed slippage
> 漏记；artifact 明示 OHLCV/L2 边界。真实 L2/queue/spread/impact/latency 校准、
> 容量置信区间、旧策略重跑与独立复核仍未实现，详见 `34`。

> Phase 3O 状态边界：FS-017/018 已在代码层完成原生 modal/focus-return、
> Escape/backdrop 与 reduced-motion CSS/JS 收口。目标浏览器 keyboard-only、
> NVDA/VoiceOver、axe、缩放、窄屏与动效人工观察仍未完成，详见 `35`。

> Phase 3P 状态边界：FS-010 的无消费者伪 auto-rollback key 已从四个 managed
> YAML、生成器和现行字段参考删除；managed loader 对非 mapping 与任何未知 Settings
> key 失败关闭，配置 reference 与生成器逐字一致且生成器不再覆盖人工治理 README。
> committed candidate 目标启动、仓库外 overlay 盘点和独立复核仍未完成，详见 `36`。

> Phase 3Q 状态边界：FS-011 旧入口已改为不加载 dotenv/runtime 的明确迁移失败
> 入口，固定 stderr 指引与 exit `2`；committed candidate 独立复核和仓库外调用方
> 迁移仍未完成，详见 `37`。

> Phase 3R 状态边界：FS-015 已用生产同名 strict boolean 收口 independent replay
> short gate；关闭时在 history/dominant/state-machine 前 short score 恒为 `0.0`，且该值
> 进入实验参数证据但不进入按 combo 发布的 active-parameter 映射。历史 artifact 重跑、
> 其他 production/replay golden vector 与独立复核仍未完成，详见 `38`。

> Phase 3S 状态边界：FS-021 已新增只读 Python 3.12 基础 workflow，自动执行全仓
> Ruff、完整 unit、strict markers 和新增 warning 阻断，并修复 Long/Short 错误 mock。
> GitHub 远端 run/required status、integration/UI/Compose/schema/security gate、SQLite
> allowlist 清零和 FS-022 供应链复现仍未完成，详见 `39`。

> Phase 3T 状态边界：FS-022 已为目标 Python 3.12/Linux x86_64 提交 runtime/CI
> 完整版本与 SHA-256 lock；Docker/CI 按 hash 安装，Python base 与九个外部 Compose
> image 固定 manifest digest，并有 verifier/contract tests。APT snapshot、clean image
> build、SBOM、CVE/license/secret/provenance、远端 CI 与独立复核仍未完成，详见 `40`。

> Phase 3V 状态边界：FS-004 已让 real-data v2 使用 train/valid 双门并把 test 限制为
> 内容 seal；最终 OOS evaluator、访问账本、历史 v1/人工查看审计、purged walk-forward、
> 多重检验、production gate 与独立复核仍未完成，详见 `42`。

> Phase 3E 状态边界：FS-009 的显式 root+RDP schema job、Batch B
> version/checksum ledger、transaction/order/rollback-suffix 契约和 managed 启动只读
> 校验已实现。空库/历史克隆/部分失败 manifest、真 Postgres 故障注入、
> app+schema 一致 rollback 和独立复核仍未完成，详见 `25`。

> Phase 3F 状态边界：FS-007 的标准部署入口已取消默认 profile，并在任何副作用前
> 硬禁用 live；关键模拟部署步骤失败关闭，future live collector 契约已补齐，模拟
> evidence 明确不构成 production/trading readiness。完整 packet、克隆部署故障矩阵、
> app+schema+parameter 一致 rollback 与独立复核仍未完成，详见 `26`。

> Phase 3G 状态边界：FS-005 的 Gateway 宿主 mapping 已固定 loopback，本地 launcher
> 拒绝 live/非 loopback，模拟 evidence 校验实际 Docker HostIp。现有容器重建、目标
> 防火墙、LAN/VPN/NAT、TLS/证书/Host/auth/cookie/限流仍未验证，详见 `27`。
>
> Phase 3H 状态边界：FS-020 已用最外层 user middleware 实施 Host 失败关闭与
> 固定 CSP/frame/nosniff/referrer/permissions/COOP/CORP 安全头，HSTS 仅对实际
> HTTPS scope 输出。目标 TLS/proxy/browser、未捕获 500 和 FS-019 auth 负载仍未验证，
> 详见 `28`。
>
> Phase 3I 状态边界：FS-019 已将同步 DB/PBKDF2/账户状态/审计完整移入
> cancel-safe 有界 worker，增加每进程 global/client/identity 限流、dummy KDF 与
> 输入/hash 上界。集中分布式限流、trusted proxy、真实 DB 和目标负载仍未验证，
> 详见 `29`。

## Phase 0 — 保持 NO-GO 与证据冻结

- 不启动 live profile，不做真实资金试单。
- 保存当前 commit、报告和逐文件矩阵；建立 P1 owner。
- 修复任务不得混入策略调参或无关重构。
- 验收：所有参与者确认 `healthy != trading-ready`，并以风险登记簿作为唯一整改入口。

## Phase 1 — 立即关闭资金控制与错误成功语义

### 1A. Profile apply/rollback（FS-001）

- 风险收敛已完成：未完成真实协议前，apply/rollback endpoint 均 fail-closed/非成功。
- 设计可恢复 reverse saga，覆盖 live payload、active set、apply history、audit 和 runtime readback。
- 测试 token/dual operator、并发 apply/rollback、部分失败恢复、幂等、目标版本不合法、读回不一致。
- 验收：只有运行值与目标版本一致才返回 succeeded；独立 reviewer 复核。

### 1B. 最终提交 fencing（FS-002）

- API 不得在缺少全部 execution worker 的 halt generation acknowledgement 时报告完成；Redis/NATS 发布故障必须可见并 fail-closed。
- 引入 kill generation/fencing token；所有风险增加 command 绑定 generation。
- 在 `place_order` 紧前最终复查，halt 使旧 generation 失效；明确允许 reduce-only。
- 确定性测试在 max-size await 中切换 halt，断言没有 outbound call。
- 验收：故障注入和并发测试通过，且不阻断合法降险订单。

## Phase 2 — 修复验证失真

### 2A. Backtest 时间契约（FS-003/014）

- 已完成因果时间层：定义 observation/decision/submit/fill timestamp，固定 next tradable event。
- 已完成 OHLCV containment：三类订单按已知 volume/cap 支持 partial fill，bounded 计 taker fee + fixed slippage，旧 fill model 不能重启。
- 待完成：真实 latency、L2 depth、spread、queue/limit touch、cancel/replace、impact 和状态依赖成本；旧证据按两个模型版本盘点并重跑。
- 用历史真实 fill 做只读校准，不向交易所下单。

### 2B. Research 数据治理（FS-004/015）

- 已完成代码层：real-data v2 的 train/valid 职责分离、双门和 test 内容 seal；test 尚未
  执行最终 OOS，也没有一次性解封控制。
- 待完成：独立 holdout evaluator/access ledger、purged walk-forward/cross-window，记录
  proposal lineage、试验次数、数据查看历史和 multiple-testing correction。
- short-bias disabled golden vector 已完成；继续建立 AI mode、cost/edge、position state
  的 replay-production golden vectors，并按显式目标 gate 重跑历史证据。
- 验收：候选不读取 test 指标做选择；最终报告明确多重试验修正和置信区间。

## Phase 3 — 统一 supervision、readiness 与部署

### 3A. Critical task supervisor（FS-006/012/016）

- 已完成第一层：显式关键 task 集合；critical task 退出触发 non-zero，FastAPI health `503`。
- 已完成第二层：七条固定周期资金关键循环用单调时钟记录成功进度；永久 await 或连续无成功周期在明确预算后触发 `stalled` 非零/503。
- 已完成启动 provisioning 代码层：四主进程 NATS/hybrid 必须等待同部署 generation 的 peer；Redis 异常/超时/旧代次失败关闭，但目标启动重启矩阵尚未验证。
- 待完成：为 WebSocket、dispatcher、abort hook、guard publisher 等事件驱动/service-owned task 定义 connection、freshness、queue lag、dependency 和 fail posture。
- 待完成：用 event-loop 外 supervisor 覆盖整体阻塞，并验证 Redis/NATS/DB/OKX 启动和持续断连对 live fail-closed。
- topic 级证明 outbox/hydrate/可丢语义。

### 3B. Trading readiness packet（FS-007）

- 内容至少包括：commit/image/schema/parameter/profile、critical tasks、account freshness、recovery、reconciliation、kill switch、command/outbox backlog、required daemons。
- 已完成风险隔离：deploy 无默认 profile，live 全部硬禁用且无 override；模拟路径先 build/validate，关键步骤失败关闭，并生成明确非 production-ready 的身份 evidence。
- 待完成后才可讨论解禁：完整 packet、保留旧 image、app+schema+parameter 一致自动/人工 rollback、克隆环境故障矩阵和独立人工批准。
- 验收：任一关键项 unknown/stale/failed 时部署非零且不能报告完成。

### 3C. 网络与浏览器边界（FS-005/019/020）

- 已完成 FS-005 代码层：Gateway 宿主端口固定 127.0.0.1，本地入口拒绝 live/非 loopback，模拟 evidence 校验实际 HostIp。
- 已完成 FS-020 代码/ASGI 层：固定本机 Host allowlist；对 HTML/JSON/error/Host 400 统一安全头；CSP 无 unsafe 例外；HSTS 仅 HTTPS。
- 已完成 FS-019 代码/隔离层：完整同步登录链有界 offload；取消不提前释放 capacity；每进程三维 limiter；dummy KDF 与输入/hash 上界。
- 待完成：在目标主机验证 runtime binding、防火墙/LAN/VPN/NAT 和真实 TLS/proxy/browser 响应；远程访问走受控代理/VPN/mTLS。
- 待完成：未捕获 500 响应头故障注入；跨进程集中限流、trusted proxy client identity、真实 DB/KDF 混合 auth 负载与慢连接/容量测试。
- 验收：静态 Compose 与 runtime inspect 都证明只绑定本机；目标 TLS/proxy 不降级 header；真实浏览器无 CSP violation；auth 负载和限流达到明确阈值。

## Phase 4 — 数据库与构建可复现

### 4A. DB 预算与迁移（FS-008/009）

- 已完成仓库静态层：建立单一连接预算和角色化主 pools；当前四进程声明 topology ceiling
  150、PostgreSQL 普通容量 197、名义余量 47；新增 engine inventory/Compose/CI 防回退。
- 待完成：生产等价全 daemon 负载、慢查询、DB 短断/重连/重启和恢复/admin 竞争；盘点或
  限制 transient/CLI/迁移/仓库外路径；建立 pool wait/timeout/利用率告警并完成数据库
  连接数 + `work_mem` 联合内存预算。根据证据再评估 PgBouncer，而不是先引入新状态层。
- 已完成代码层：禁止 managed 应用 runtime DDL；部署期显式 migrate job + root/RDP 版本/checksum ledger；Gateway 失败不 ready。
- 待完成：克隆库演练 upgrade/rollback/partial failure，校验表、列、约束、索引、view/function、checksum，并证明 app+schema 一致回退。

### 4B. CI/供应链（FS-021/022）

- 已完成仓库内基础：Python 3.12/Linux runtime + CI lock/hashes、Python base 和九个外部
  image digests、防回退 verifier/tests。
- 待完成：APT snapshot、clean build、SBOM、CVE/license/secret/signature/provenance scan。
- 已建立 Python 3.12 lint/unit/strict-marker/新增-warning 基础 gate；待远端设为 required
  status check，并补 config/schema/UI/integration 分层 gate；warning allowlist 逐步归零。
- 验收：干净环境从同 commit 得到相同依赖和镜像；必需检查自动阻断合并/release。

## Phase 5 — 可访问性、配置和旧入口

- 已完成代码/静态单测：原生 dialog/focus contract、Escape/return focus 与 reduced-motion（FS-017/018）；待完成目标浏览器键盘/读屏/axe/缩放/动效人工验证。
- 已完成代码/隔离单测：删除无消费者伪键，并在 managed loader 对 runtime defaults、
  strategy YAML 非 mapping/unknown keys 失败关闭（FS-010）；待完成目标启动、仓库外
  overlay 盘点、generator clean-run 与独立复核。
- 已完成代码/隔离单测：`run_local.py` 提供无配置副作用的明确迁移错误（FS-011）；
  待完成仓库外调用方迁移与独立复核。

## 最终上线验收

只有以下条件同时满足才可重新评估 NO-GO：

1. 所有 P1 关闭且有独立复核证据；P2 有明确关闭或正式风险接受人。
2. unit/lint/integration/故障注入/迁移/容量/可访问性验证均记录真实命令和结果。
3. 隔离 4-proc release candidate 连续 soak，无 critical task、reconciliation、outbox、DB/NATS 告警。
4. 只读 live preflight 证明账户模式、权限、持仓/订单、kill switch、参数、schema、备份与告警；不提交真实订单。
5. 回滚演练能够恢复应用版本、schema/参数版本和安全 halt 状态。

若任一项为 UNKNOWN，结论仍是 NO-GO。
