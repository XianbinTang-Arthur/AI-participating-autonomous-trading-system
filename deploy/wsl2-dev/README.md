# AATS WSL2 本地基础设施

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 多进程切片化部署所需的全部本地基础设施。
> 全部组件运行在 WSL2 (Ubuntu) 的 docker compose 内，**零云费用**。
> 本环境存在的意义，是为“长期稳定盈利、服务 AI 资本积累”的交易系统提供本地验证、演练与观测底座；完整定位见 [../../docs/project_positioning.md](../../docs/project_positioning.md)。

---

> 当前约定：`.env.wsl2` 的单一真相放在仓库根目录，例如 `~/aats/.env.wsl2`。
> `scripts/deploy.sh` 仍兼容旧位置 `deploy/wsl2-dev/.env.wsl2`，但只建议作为迁移期兼容路径。
>
> 最后核对：2026-08-22（代码基线 `be9179e`）。本页说明基础设施构成；部署唯一入口仍是仓库根目录的 `scripts/deploy.sh`。

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
| Grafana  | 10.4.4 | 3000        | 512M    | 4 数据源统一看板 + 5 条告警规则       | volume `grafana_data`   |

基础设施合计约 **7.2 GB** 内存（9 个服务）。

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
bash scripts/deploy.sh --profile derivatives-live --skip-commit
```

部署报告完成后可做只读验证：

```bash
# 必要时确认 Windows 与 WSL2 checkout 对齐
bash scripts/sync_to_wsl2.sh check

# 基础设施探针
docker exec aats-postgres pg_isready -U aats  # Postgres
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
bash scripts/deploy.sh --profile derivatives-live --skip-commit

# 确认 Windows 与 WSL2 checkout 是否一致
bash scripts/sync_to_wsl2.sh check
```

---

## 连接配置边界

这些连接信息由 `scripts/deploy.sh` 通过根目录 `.env.wsl2` 和 profile env 文件注入。
不要再维护单独的旧式 WSL2 dev env 文件或手动启动 4 个进程。

当前 Compose 公共环境使用 `AATS_DATABASE_URL`、`AATS_HOT_STATE_REDIS_URL`、`AATS_NATS_URL`、`AATS_ACTIVE_PARAMETER_DB_URL` 和 `AATS_OTEL_*`。连接串可能包含凭证，本文不复制示例值；以 `configs/templates/` 的无密钥模板和 Compose 声明为字段参考，以根目录受忽略的真实环境文件为运行输入。

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

- 全部端口仅监听 `127.0.0.1`，**不会**暴露到局域网或公网
- `.env.wsl2` 已在 `.gitignore` 排除，密码不会泄露到 git
- 发布和重启统一使用 `scripts/deploy.sh`；不要把本文的基础设施细节当作第二套部署入口
- NATS dev 配置未启用生产级认证/多节点 HA；只适合本机演练
- live profile 即使能在本地跑起来，也必须按 `DEPLOYMENT.md` 和 Operator checklist 完成 trading-ready 检查。

### 使用边界

本 WSL2 栈默认只作为开发、模拟盘、演练和观测环境。若用于 live 演练，必须额外确认 profile、Operator auth、database、OKX account、kill switch、reconciliation、active parameter history 和 recovery status。

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
