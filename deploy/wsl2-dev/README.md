# AATS WSL2 本地基础设施

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 多进程切片化部署所需的全部本地基础设施。
> 全部组件运行在 WSL2 (Ubuntu) 的 docker compose 内，**零云费用**。
> 本环境存在的意义，是为“长期稳定盈利、服务 AI 资本积累”的交易系统提供本地验证、演练与观测底座；完整定位见 [../../docs/project_positioning.md](../../docs/project_positioning.md)。

---

> 当前约定：`.env.wsl2` 的单一真相放在仓库根目录，例如 `~/aats/.env.wsl2`。
> `scripts/deploy.sh` 仍兼容旧位置 `deploy/wsl2-dev/.env.wsl2`，但只建议作为迁移期兼容路径。
>
> 最后核对：2026-08-25（Git 基线 `00b6df0` + 当前未提交 Phase 3A–3V 工作区覆盖层）。本页说明基础设施构成；部署唯一入口仍是仓库根目录的 `scripts/deploy.sh`。静态文档不能证明当前容器、网络、数据库、账户、交易所或风控状态。

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

profile 必填；当前只允许 `spot`/`derivatives`，所有 live profile 在副作用前失败且无 override。部署顺序由该入口固定为：预检与同步 → 生成本次 runtime readiness generation → 构建新镜像 → 停止旧栈 → 启动并检查基础设施 → 执行一次性 root + RDP schema migration job → 启动应用 → 应用健康检查 → 写入模拟证据包。四主进程只有在同 generation peer 就绪后启动 NATS/hybrid publisher；该代次同时写入 evidence。应用进程只做 schema exact validation，不得在 lifespan 或 daemon 启动路径隐式建表、补列或迁移。任一关键步骤失败必须中止；模拟证据明确不是 trading-ready，也不证明生产库已迁移或 NATS 目标故障矩阵已通过。

部署报告完成后可做只读验证：

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
