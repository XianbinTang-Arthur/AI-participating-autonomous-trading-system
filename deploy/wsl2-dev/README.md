# AATS WSL2 本地基础设施

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 多进程切片化部署所需的全部本地基础设施。
> 全部组件运行在 WSL2 (Ubuntu) 的 docker compose 内，**零云费用**。
> 本环境存在的意义，是为“长期稳定盈利、服务 AI 资本积累”的交易系统提供本地验证、演练与观测底座；完整定位见 [../../docs/project_positioning.md](../../docs/project_positioning.md)。

---

> 当前约定：`.env.wsl2` 的单一真相放在仓库根目录，例如 `~/aats/.env.wsl2`。
> `scripts/deploy.sh` 仍兼容旧位置 `deploy/wsl2-dev/.env.wsl2`，但只建议作为迁移期兼容路径。
>
> 文档状态：现行基础设施与部署入口说明
> 最后核对：2026-08-29（核对基线 `main@f9bb24996436` + 当前 FS-016 NATS exact-ownership 候选；以本文档所在最终 HEAD 为准）。本页说明基础设施构成；部署唯一入口仍是仓库根目录的 `scripts/deploy.sh`。静态文档不能证明当前容器、网络、数据库、账户、交易所或风控状态。

## 拓扑

```
              ┌────────────────────────────────────────────────────┐
              │                  WSL2 Ubuntu                       │
              │                                                    │
              │   ┌──────────┐  ┌────────┐  ┌──────────────────┐   │
              │   │ Postgres │  │ Redis  │  │ NATS JetStream   │   │
              │   │  :5432   │  │ :6379  │  │  :4222 / :8222   │   │
              │   └──────────┘  └────────┘  └──────────────────┘   │
              │                                                    │
              │   ┌──────────┐  ┌────────┐  ┌──────────────────┐   │
              │   │  Loki    │  │ Jaeger │  │     Grafana      │   │
              │   │  :3100   │  │ :16686 │  │      :3000       │   │
              │   └──────────┘  └────────┘  └──────────────────┘   │
              │                                                    │
              │   ┌──────────┐  ┌────────────────┐  ┌──────────┐  │
              │   │Prometheus│  │ Redis-Exporter │  │ Promtail │  │
              │   │  :9090   │  │     :9121      │  │  :9080   │  │
              │   └──────────┘  └────────────────┘  └──────────┘  │
              │                                                    │
              └──────────────────────┬─────────────────────────────┘
                                     │ 127.0.0.1 端口转发
                          ┌──────────┴──────────┐
                          │ AATS 主交易切片      │
                          │ gateway / market   │
                          │ decision / exec    │
                          │ + RDP/采集 daemons │
                          └─────────────────────┘
```

| 组件     | 版本 | 端口          | 内存限制 | 用途                                 | 持久化                  |
|----------|------|---------------|---------|--------------------------------------|-------------------------|
| Postgres | 16   | 5432          | 2560M   | 主存储（账务、订单、策略状态）       | docker volume `postgres_data` |
| Redis    | 7    | 6379          | 512M    | 跨进程热状态缓存（仓位、订单视图）   | AOF, volume `redis_data` |
| Redis-Exporter | 1.58.0 | 9121 | 64M    | Redis 指标采集 → Prometheus          | 无                       |
| NATS     | 2.10 | 4222 / 8222   | 1024M   | JetStream 跨进程事件总线             | volume `nats_data`      |
| Loki     | 3.0  | 3100          | 512M    | 日志聚合（7 天保留）                 | volume `loki_data`      |
| Promtail | 3.0  | 9080          | 256M    | Docker 容器日志采集 → Loki           | volume `promtail_positions` |
| Jaeger   | 1.57 | 16686 / 4317 / 4318 | 1536M | 分布式 trace（OTLP gRPC + HTTP） | volume `jaeger_badger_data` |
| Prometheus | 2.51 | 9090         | 256M    | AATS 进程指标采集                    | volume `prometheus_data` |
| Grafana  | 12.4.3 | 3000        | 512M    | 4 数据源统一看板 + 5 条告警规则       | volume `grafana_data`   |

基础设施合计约 **7.2 GB** 内存（9 个服务）。

Phase 3T 起，上表九个 registry image 均在 Compose 中使用可读 tag 加 manifest digest，
Python 两个 build stage 也固定 `python:3.12-slim` digest；运行时第三方 Python wheel 从
`requirements/runtime-py312-linux-x86_64.lock` 按 SHA-256 安装。本地构建的
`aats-base:dev` 由部署证据记录实际 image ID。该机制防止 tag 和 Python 解析静默漂移，
不等价于 SBOM、漏洞/许可证/签名审计或成功构建；APT 与上述供应链门仍开放。

Phase 3U 起，PostgreSQL 显式声明 `max_connections=200`、reserved=3；应用当前四进程完整
声明 topology ceiling=150、普通连接容量 197、名义余量 47。该算术只由
`scripts/verify_database_connection_budget.py` 做静态一致性检查，不证明目标负载、故障
重连、瞬时 CLI/迁移/恢复/admin 或 `work_mem` 联合内存已经通过。

---

## 前置要求

1. **Windows 11 已启用 WSL2**
2. WSL2 Ubuntu 内 Docker Engine/Compose 可用；本仓库当前以独立 WSL2 Docker 环境为准，不要求通过 Docker Desktop 管理
3. **磁盘可用空间 ≥ 5GB**
4. WSL2 内已经能 `docker compose version`

---

## 第一次拉起

先在 WSL2 native checkout 中准备基础设施环境文件：

```bash
cd ~/aats
cp configs/templates/.env.wsl2.example .env.wsl2
$EDITOR .env.wsl2     # 把 *_change_me 改成你自己的值
```

然后回到 Windows 工作区根目录，通过唯一部署入口执行：

```bash
bash scripts/deploy.sh --profile derivatives --skip-commit
```

profile 必填；当前只允许 `spot`/`derivatives`，所有 live profile 在副作用前失败且无 override。profile/live gate 后、任何 mutation 前，入口取得由单一长寿命 WSL `flock` holder 持有的全流程锁。生产路径固定为 `/tmp/aats-standard-deploy.lock`；`AATS_DEPLOY_LOCK_FILE` 仅在 `AATS_DEPLOY_TEST_MODE=true` 的隔离测试可覆盖，生产设置会失败关闭。Windows 每 3 秒刷新 lease，holder 连续 12 秒未见刷新才释放 fd，锁不通过删除 inode 释放；新 holder 取得 flock 后仍等待任何 fresh predecessor lease 清除或超过 12 秒，再报告 `ACQUIRED`。每个外部步骤 spawn 前复核 holder/heartbeat/flock，spawn 后全局登记 active child PID/context；活动中失锁会终止并 wait 进程树。`TERM/HUP/INT/EXIT` 也必须先完成该子进程清理，再停止 heartbeat、移除 lease 并释放/wait holder flock。部署顺序固定为：持锁预检与同步 → 生成本次 runtime readiness generation → 构建新镜像并校正 WSL2 `vm.overcommit_memory=1` → 以 15 秒 stop budget 停止七个已知应用并建立 quiescence 基线 → 受控启动基础设施-only → 第一次 loopback 全量分页只读 NATS durable cutover preflight → PASS 后以 5 秒 down budget full-down 并建立新基线 → 启动并检查基础设施 → 执行一次性 root + RDP schema migration job → app up 前第二次同等 preflight → 启动应用 → 在默认 210 秒预算内完成应用健康检查 → 将两次 preflight chain、相对路径与 hash 写入模拟证据包 → 仍在持锁状态下输出报告。锁竞争/失锁、Redis 前置条件无法校正或任一阶段失败都失败关闭；Postgres 探针同时指定容器内用户和基础数据库，不会向不存在的用户名同名库探测。

四主进程的 strict NATS/hybrid readiness 为 Redis-only protocol v2。每个 role 在任何 NATS I/O 前以全局 `aats:runtime:owner:<role>` key claim `PROVISIONING`，立即开始续租并启动独立 subprocess watchdog，然后完成 55 秒 takeover quarantine；generation 只位于 payload/peer barrier。父子进程统一使用 POSIX `CLOCK_BOOTTIME` / Windows `GetTickCount64`，并以 POSIX pidfd / Windows process creation FILETIME 固定父进程身份。build 完成后 owner-aware CAS 为 `READY`，所有同 generation peer `READY` 后先 flush 最多 4,096 条且 64 MiB 的 build 期 publish 缓冲，再开放 callback 和 background tasks。仅该 strict 分布式路径注入 delivery gate 并把 push consumer 的 `max_ack_pending` 固定为 `1`；non-strict、in-memory 和 monolith 不注入 gate、也不强制该窗口。checker/runtime 共用 mutable migration policy：只允许 snapshot/transient 正数旧 `ack_wait` 向声明目标提高；event `ack_wait`、任意 `max_deliver` 和不安全非事件 drift 阻断。critical ALL cursor 不得删除。现存 durable 在 update/bind 前冻结 created、真实 inbox 与四维 cursor，回读/post-bind/READY/steady supervision 均拒绝同名重建、inbox 变化或 cursor 回退；实际 inbox 不进入日志/证据。flow control/idle heartbeat 历史差异当前只保留并告警。

首次将旧/无限窗口降到 `1` 时，标准入口按所有受支持 profile 应用容器的并集 stop（避免 derivatives -> spot 遗留 collector），并只接受 `exited/dead` 或明确 not-found；paused/restarting/removing/unknown/inspect 失败均阻断。quiescence 证据覆盖七容器的 ID、状态、`StartedAt`、`FinishedAt`、`RestartCount`，preflight 前后还查询精确时间窗内的 Docker lifecycle events。入口以 base Compose 受控确保基础设施/NATS 在线而不启动 app，再由 `~/aats-venv` 固定从 loopback 分页读取全部 stream/consumer。schema v3 使用 `consumer_ownership.py` 人工 authoritative declaration，并由动态 assembly 测试证明与四个 `build_runtime()` 精确一致：角色 `31/8/27/11`、语义 `49/24/4`，共 `77`。检查分页/总数、逐 stream consumer count、exact identity/role/topic/semantics、created、四维 cursor、safety-projection config、窗口和 outstanding；实际 inbox 只记录存在性。preserved install 必须 exact `77` 且无 unowned；fresh install preflight 必须为空，app-up 后 final 才必须 exact `77`。标准 stop 只证明生产者已停，不证明 drain；旧/无限窗口仍有任何未 ACK、窗口已为 `1` 但 outstanding 大于 `1`、配置 drift、查询失败或 quiescence 变化都会阻断并保持 NATS 在线。它绝不自动 ACK/delete/update/recreate/reset/purge；outstanding 只能经人工批准后用匹配旧消费者自然 drain，missing/unowned/config drift 必须 release review。第一次 PASS 后才 full-down；重建基线并完成正常 infra/schema 后，app up 前必须再次 PASS。v3 证据绑定 lock id、generation、deployed commit 与 quiescence，最终 evidence 重算 continuity、记录 path/hash，并封存两次 no-secret canonical durable projection/hash。

LAST/NEW 的 ACK-window backlog 重建只在 strict delivery gate、policy 非 ALL，并且（outstanding 超过目标，或窗口正在收缩且 outstanding 非零）时发生；safety-projection immutable drift 另有声明丢弃语义重建分支。两者都保持 LAST 仅取最新、NEW 仅收未来，自动重启也可能触发；标准 preflight 会先阻断 immutable drift，full-down 是首次切换的额外发布门禁，不是运行时代码输入。ALL cursor 不适用。

TTL 为 60 秒，每 10 秒续租，safety margin 30 秒，hard-exit grace 10 秒。每次成功 `PROVISIONING` 写入/续租后，本地 hard fence 最多滑动到该次写入后 50 秒；另有 claim 到 `READY` 的 180 秒绝对 promotion 上界，续租不得延长，并在第 170 秒冻结续租、进入 10 秒 fatal grace。确定失租零宽限。关键故障立即冻结续租并进入不可解除的 fatal deadline；正常 cleanup 也先冻结续租并受 10 秒硬截止保护，业务/NATS 安全停止后才 disarm、owner-aware delete。Redis 配置为 `noeviction`，写满显式失败；NATS 连续断连 30 秒进入 runtime critical failure。critical durable 明确缺失、created/inbox/name/safety-projection config drift 或四维 cursor 回退立即 terminal；management、push binding、heartbeat 持续失败 30 秒 terminal；有 backlog 且进度在 `max(30 秒, 2 x ack_wait)` 内不变也 terminal。post-bind 失败只 abort gate 并有界取消本地 subscription，不 drain/ACK/delete broker durable。该代次同时写入 evidence。

protocol v1 -> v2 首次发布和回滚禁止 rolling/mixed version，只能走标准 full-down/full-up；旧 gateway/market/decision/execution 未全部停止，或首次 ACK-window cutover 未取得完整只读 PASS 时，均不得 app up。NATS 八键目标在同步后冻结为 root-owned `0444` 白名单快照，并作为所有必需应用最后一个 `env_file`；preflight、容器 manifest label、健康边界和最终证据必须同摘要。应用健康后另执行 40 秒主动观察，最终证据逻辑窗只能为 35–60 秒。应用进程只做 schema exact validation，不得在 lifespan 或 daemon 启动路径隐式建表、补列或迁移。任一关键步骤失败必须中止；模拟证据明确不是 trading-ready。真实 NATS consumer-delete 集成已经证明 durable 消失会在 core TCP 仍连通时触发 terminal，当前候选全量单测、preflight 聚焦回归和隔离 smoke 已通过；但尚未执行 v3 标准部署/完整 Docker 矩阵，当前 lease 也仍不是下游执行端强制校验的单调 fencing token。2026-08-29 03:43Z 标准尝试仍运行已提交 `f9bb2499` 旧 v2 checker：扫描 `78`、只识别 `49`，把 `28` 个合法非事件 consumer 与一个 `aats-codex_manual_resume-system_operator_command_responses` 一并列为 `29` unexpected 后阻断。候选 v3 的非发布只读诊断得到 `77+1`，仍须提交后标准重跑；七个 app 保持停止，基础设施在线，未知 durable 不得自动删除。真 Redis/NATS/Docker 故障注入、标准发布、网络/push/heartbeat/backlog stall、双故障、下游 fencing 与受控逐角色重启矩阵仍 `OPEN`，真实资金继续 `NO-GO`。

部署报告完成后可做只读验证：

`derivatives` 模拟 profile 还会启动 `aats-liquidations-daemon` 和
`aats-microstructure-collector` 两个公共数据采集器，应用必需集合因此为七个容器。采集器只写
研究库、不加载 live env、不接 execution command；其 60 秒 heartbeat 是部署证据的一部分，
但不能代替 Silver 表数据新鲜度和 eligibility。完整流程见
[收益证据与模拟交易就绪运行手册](../../docs/operations/profit_readiness_runbook.md)。

```bash
# 必要时确认 Windows 与 WSL2 checkout 对齐
bash scripts/sync_to_wsl2.sh check

# 基础设施探针
docker exec aats-postgres sh -lc 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'  # Postgres
docker exec aats-redis redis-cli ping         # Redis
curl http://localhost:8222/healthz             # NATS
curl http://localhost:3100/ready               # Loki
curl http://localhost:9090/-/healthy           # Prometheus
curl http://localhost:3000/api/health          # Grafana
curl -s http://localhost:16686/ | head -1      # Jaeger UI
```

预期所有服务的 `STATUS` 应为 `Up (healthy)`。

---

## 日常使用

```bash
# 发布 / 重建 / 健康检查
bash scripts/deploy.sh --profile derivatives --skip-commit

# 确认 Windows 与 WSL2 checkout 是否一致
bash scripts/sync_to_wsl2.sh check
```

不要直接调用 `scripts/rdp_init_db.py` 或逐 stage 工具替代部署期复合迁移；前者是显式管理操作，后者只用于经批准的局部修复。标准路径是 `scripts/deploy.sh` 内部调用 `scripts/apply_schema_migrations.py`。

---

## 连接配置边界

这些连接信息由 `scripts/deploy.sh` 通过根目录 `.env.wsl2` 和 profile env 文件注入。
不要再维护单独的旧式 WSL2 dev env 文件或手动启动 4 个进程。

当前 Compose 公共环境使用 `AATS_DATABASE_URL`、`AATS_HOT_STATE_REDIS_URL`、`AATS_NATS_URL`、`AATS_ACTIVE_PARAMETER_DB_URL`、`AATS_RUNTIME_READINESS_GENERATION` 和 `AATS_OTEL_*`。generation 由 deploy 脚本临时生成，不写入 `.env.*`；缺失时 Compose 失败。连接串可能包含凭证，本文不复制示例值；以 `configs/templates/` 的无密钥模板和 Compose 声明为字段参考，以根目录受忽略的真实环境文件为运行输入。

> 注意：`AATS_DATABASE_SINGLE_RUNTIME_GUARD_ENABLED=true` 是允许打开的 ——
> 改造后的 `scoped_runtime_lock_key` 会按 `AATS_PROCESS_ROLE` 派生不同的 advisory lock，
> 4 个进程之间互不阻塞，但每个 role 还是只能有一份在跑（防止重复启动同名进程）。

---

## Grafana 登录

- URL: <http://localhost:3000>
- 默认账号：`.env.wsl2` 里的 `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`
- 数据源（已自动注入）：
  - Loki — 默认数据源，结构化 JSON 日志查询
  - Jaeger — 与 Loki 双向 trace-log 跳转
  - Prometheus — AATS 进程指标 + Redis 延迟
  - Postgres — 直接 SQL 查 AATS 业务表
- 仪表盘（已自动注入）：
  - **AATS Operations** — 进程心跳、决策频率、Fill 成功率、Redis 延迟、对账异常
  - **AATS Logs Overview** — 按级别/进程/容器聚合日志量 + 全日志搜索
- 告警规则（已自动注入）：
  - SEV1: Kill Switch 触发 / Reconciliation 不一致
  - SEV2: 进程崩溃 (Traceback) / 决策周期停滞
  - SEV3: 错误率过高 (15 分钟内 > 5 条 ERROR)

---

## 数据备份

使用随栈提供的脚本：

```bash
cd ~/aats/deploy/wsl2-dev
RETENTION_DAYS=14 ./scripts/backup_postgres.sh
```

恢复前必须先保留当前备份并按 [部署与运行说明](../../DEPLOYMENT.md) 停止应用，再执行：

```bash
./scripts/restore_postgres.sh latest
```

---

## 故障排查

| 现象 | 排查 |
|------|------|
| `port is already allocated` | Windows 上已经有进程占用对应 127.0.0.1 端口；用 `netstat -ano \| findstr :5432` 找到并停止 |
| Postgres 健康检查失败 | 查看 `aats-postgres` 容器状态/日志并核对磁盘与 volume；不要删除 volume |
| NATS JetStream 报 `no space left` | 先检查三条 stream 和 8 GiB server 容量；运行 `scripts/nats_stream_migrate.py --dry-run` 评估配置漂移。不得通过调小上限或删除 volume 处置 |
| Grafana 看不到数据源 | 查看 `aats-grafana` 日志和 provisioning 文件；修复后通过 `scripts/deploy.sh` 重建，不手动 restart Compose 服务 |
| Loki 报 `entry too far behind` | 主机时间不同步，进 WSL2 `sudo ntpdate ntp.aliyun.com` |

---

## 安全说明

- Compose 中全部宿主 published port（含 Gateway）固定到 `127.0.0.1`；模拟部署 evidence 还会拒绝实际 Gateway HostIp 漂移。静态配置不能单独证明既有容器已重建或目标宿主防火墙/NAT/VPN 不可达
- 需要远程访问时必须使用另行批准的 proxy/VPN/mTLS 设计，不得把端口映射改回 all-interface
- `.env.wsl2` 已在 `.gitignore` 排除，密码不会泄露到 git
- 发布和重启统一使用 `scripts/deploy.sh`；不要把本文的基础设施细节当作第二套部署入口
- NATS dev 配置未启用生产级认证/多节点 HA；只适合本机演练
- 当前标准入口硬禁用 live profile；不得直接 Compose 绕过。重新开放必须按 `DEPLOYMENT.md` 完成全部 gate、克隆回滚演练和独立复核。

### 使用边界

本 WSL2 栈当前只用于开发、模拟盘、演练和观测。标准入口不允许 live 演练；未来重新开放前必须额外确认 profile、Operator auth、database、OKX account、kill switch、reconciliation、active parameter history、recovery status 和一致回滚。

---

## 与多进程切片化的关系

| Stage | 用到的组件 | 说明 |
|-------|-----------|------|
| Stage 1 | Postgres | 把 Postgres 搬到 WSL2，替代旧的 host 进程 |
| Stage 2 | Postgres | 加 per-role advisory lock，准备多进程启动 |
| Stage 4 | NATS | 引入 HybridEventBus，逐步把 in_memory 事件迁过去 |
| Stage 5 | NATS | 三条 JetStream 分层：MARKET、EVENTS、COMMANDS；`audit.records` 直接持久化到 Postgres |
| Stage 6 | Redis | 跨进程热状态缓存 + WebSocket session |
| Stage 8 | Loki + Jaeger + Grafana | 4 进程统一可观测性（OTel trace + 结构化日志）|
| Stage 9 | Prometheus + Promtail + Redis-Exporter | 指标采集 + 5 条告警规则 + 2 仪表盘 |

每个 stage 的具体改动见 `ARCHITECTURE.md` 和 git 提交记录。
