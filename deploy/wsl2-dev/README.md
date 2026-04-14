# AATS WSL2 本地基础设施

> 多进程切片化部署所需的全部本地基础设施。
> 全部组件运行在 WSL2 (Ubuntu) 的 docker compose 内，**零云费用**。

---

> 当前约定：`.env.wsl2` 的单一真相放在仓库根目录，例如 `~/aats/.env.wsl2`。
> `scripts/deploy.sh` 仍兼容旧位置 `deploy/wsl2-dev/.env.wsl2`，但只建议作为迁移期兼容路径。

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
                          │  AATS 4 个进程       │
                          │  gateway / market   │
                          │  decision / exec    │
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
2. **已安装 Docker Desktop** 并在 Settings → Resources → WSL Integration 里把目标 Ubuntu 发行版打开
3. **磁盘可用空间 ≥ 5GB**
4. WSL2 内已经能 `docker compose version`

---

## 第一次拉起

```bash
# 1. 进入目录（用 WSL2 路径，不要用 /mnt/d/...）
cd ~/aats

# 2. 复制环境变量模板并修改密码
cp configs/templates/.env.wsl2.example .env.wsl2
$EDITOR .env.wsl2     # 把 *_change_me 改成你自己的值

# 3. 拉起全部服务
cd deploy/wsl2-dev
docker compose --env-file ../../.env.wsl2 up -d

# 4. 看一下健康状况（首次启动 ~30s）
docker compose --env-file ../../.env.wsl2 ps

# 5. 验证关键端口
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
# 启动
docker compose --env-file ../../.env.wsl2 up -d

# 查看日志（跟踪一个服务）
docker compose logs -f postgres

# 重启某个服务
docker compose restart nats

# 停止全部（保留数据）
docker compose down

# 完全清理（删除全部数据，慎用！）
docker compose down -v
```

---

## 连接信息（AATS 进程侧 .env）

把下面的内容追加到 `D:/文件/project/.../envs/.env.wsl2-dev` （或你实际使用的 .env），然后让 4 个进程加载：

```bash
# Postgres
AATS_STORAGE_MODE=postgres
AATS_DATABASE_URL=postgresql+psycopg://aats:aats_dev_change_me@127.0.0.1:5432/aats?options=-csearch_path%3Dpublic
AATS_DATABASE_AUTO_CREATE_SCHEMA=true
AATS_DATABASE_SINGLE_RUNTIME_GUARD_ENABLED=true

# Redis（Stage 6 引入）
AATS_REDIS_URL=redis://127.0.0.1:6379/0

# NATS（Stage 4 引入）
AATS_NATS_URL=nats://127.0.0.1:4222
AATS_EVENT_BUS_BACKEND=hybrid       # in_memory | hybrid | nats

# OpenTelemetry（Stage 8 引入）
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317
OTEL_SERVICE_NAME=aats
```

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

参见 `../scripts/backup_postgres.sh`（Stage 1 同步交付的另一脚本）。

简易手动备份：

```bash
docker exec aats-postgres pg_dump -U aats -F c -d aats > backup_$(date +%F).dump
```

恢复：

```bash
cat backup_2026-04-07.dump | docker exec -i aats-postgres pg_restore -U aats -d aats --clean --if-exists
```

---

## 故障排查

| 现象 | 排查 |
|------|------|
| `port is already allocated` | Windows 上已经有进程占用对应 127.0.0.1 端口；用 `netstat -ano \| findstr :5432` 找到并停止 |
| Postgres 健康检查失败 | `docker compose logs postgres`，常见原因：上次 `down -v` 没干净，留有旧 volume 残文件 |
| NATS JetStream 报 `no space left` | 编辑 `nats/nats-server.conf`，调小 `max_file_store` 或 `down -v` 重置 |
| Grafana 看不到数据源 | 容器启动顺序问题；`docker compose restart grafana` |
| Loki 报 `entry too far behind` | 主机时间不同步，进 WSL2 `sudo ntpdate ntp.aliyun.com` |

---

## 安全说明

- 全部端口仅监听 `127.0.0.1`，**不会**暴露到局域网或公网
- `.env.wsl2` 已在 `.gitignore` 排除，密码不会泄露到 git
- **永远不要**直接在生产环境复用这套 compose；这只是开发栈
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
| Stage 5 | NATS | 全量切到 file storage，关键 topic 持久化 |
| Stage 6 | Redis | 跨进程热状态缓存 + WebSocket session |
| Stage 8 | Loki + Jaeger + Grafana | 4 进程统一可观测性（OTel trace + 结构化日志）|
| Stage 9 | Prometheus + Promtail + Redis-Exporter | 指标采集 + 5 条告警规则 + 2 仪表盘 |

每个 stage 的具体改动见 `ARCHITECTURE.md` 和 git 提交记录。
