# AATS 生产发布全流程

> 本文档描述从代码变更到生产运行的完整部署管线，包括构建、同步、容器编排、健康检查、备份与监控。

---

## 一、整体架构总览

```
Windows 开发端                     WSL2 Docker 运行端
+----------------+   git sync    +------------------------------------------------+
|  代码编辑       |-------------->|  ~/aats (git repo clone)                        |
|  D:\文件\       |  sync_to_    |                                                |
|  project\      |  wsl2.sh     |  deploy/wsl2-dev/                              |
|  AATS\         |   pull       |  |- docker-compose.yml        (基础设施)         |
+-------+--------+              |  |- docker-compose.aats.yml   (4进程应用)        |
        |                       |  |- docker-compose.aats.*.yml (profile叠加)     |
        | deploy.sh             |  +- Dockerfile                                 |
        | (一键部署)             |                                                |
        +---------------------->|  docker compose build -> up -d -> healthcheck   |
                                +------------------------------------------------+
```

---

## 二、一键部署管线（`scripts/deploy.sh`）

### 7 步自动化流水线

| 步骤 | 动作 | 说明 |
|------|------|------|
| 1 | `git commit` | 可选 `--commit "msg"`，有 dirty tree 才提交 |
| 2 | WSL2 同步 | `scripts/sync_to_wsl2.sh pull`（git fetch + merge --ff-only） |
| 3 | 停旧服务 | `docker compose down` |
| 4 | 构建镜像 | `docker compose build`（可选 `--no-cache`） |
| 5 | 清理旧镜像 | `docker image prune -f` |
| 6 | 启动基础设施 | `docker-compose.yml` 单独启动 Postgres/Redis/NATS/Loki/Jaeger/Grafana |
| 7 | 启动应用 + 健康检查 | 应用层 compose overlay 启动 4 进程 + rdp-daemon，轮询 `/healthz` 最长 90s |

### 支持的 Profile

| Profile | 用途 | API 端口 | 数据库 |
|---------|------|----------|--------|
| `spot` | 现货模拟 | 8000 | aats_spot |
| `spot-live` | 现货实盘 | 8001 | aats_live_spot |
| `derivatives` | 衍生品模拟 | 8010 | aats_derivatives |
| `derivatives-live` | **衍生品实盘（默认）** | 8011 | aats_live_derivatives |
| `derivatives-live-monolith` | 单进程兜底模式 | 8011 | aats_live_derivatives |

### 用法

```bash
# 常规部署（带提交信息）
./scripts/deploy.sh --profile derivatives-live --commit "fix edge alignment"

# 强制重新构建镜像
./scripts/deploy.sh --profile derivatives-live --no-cache

# 紧急回滚到单进程模式
./scripts/deploy.sh --profile derivatives-live-monolith
```

### 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 参数错误 |
| 2 | 同步 / 构建 / 启动失败 |
| 3 | 健康检查超时 |

---

## 三、Windows 与 WSL2 代码同步（`scripts/sync_to_wsl2.sh`）

| 模式 | 作用 |
|------|------|
| `init` | 首次从 Windows `/mnt/d/...` 克隆到 WSL2 `~/aats` |
| `pull` | 增量同步：`git fetch + merge --ff-only`（拒绝非快进） |
| `check` | 对比两侧 git HEAD |
| `status` | WSL2 侧 git status + log |
| `path` | 输出 WSL2 项目绝对路径 |
| `shell` | 直接进入 WSL2 项目目录 bash |

**设计原则**：只同步已 commit 的状态，工作树改动不同步（有意为之，防止未完成的修改进入构建）。

---

## 四、Docker 构建层

### Dockerfile（`deploy/wsl2-dev/Dockerfile`）

两阶段构建，基于 Python 3.12-slim：

| 阶段 | 内容 |
|------|------|
| builder | build-essential -> venv -> `pip install -e ".[nats,redis,otel]"` |
| runtime | 复制 venv -> 安装 curl + tini -> 创建非 root 用户 `aats`(UID 1000) |

关键设计：

- **ENTRYPOINT**：`["/usr/bin/tini", "--"]`（PID 1 信号转发，避免僵尸进程）
- **默认 CMD**：`uvicorn apps.api_gateway.main:app --host 0.0.0.0 --port 8000`
- **单一镜像 + 不同入口**：4 个服务使用同一镜像，通过 `AATS_PROCESS_ROLE` 环境变量区分角色

---

## 五、Docker Compose 拓扑

### 基础设施层：`docker-compose.yml`

| 服务 | 版本 | 角色 | 端口 | 内存限制 | 持久化 |
|------|------|------|------|---------|--------|
| **aats-postgres** | PG 16 | 关系数据库（5 个隔离库） | 127.0.0.1:5432 | 2560M | postgres_data volume |
| **aats-redis** | Redis 7 | 热状态 KV 缓存 | 127.0.0.1:6379 | 512M | redis_data volume |
| **aats-redis-exporter** | 1.58.0 | Redis 指标采集 → Prometheus | 127.0.0.1:9121 | 64M | 无 |
| **aats-nats** | NATS 2.10 | JetStream 事件总线 | 127.0.0.1:4222 / 8222 | 1024M | nats_data volume |
| **aats-loki** | Loki 3.0 | 集中式日志聚合（7 天保留） | 127.0.0.1:3100 | 512M | loki_data volume |
| **aats-promtail** | 3.0.0 | Docker 容器日志采集 → Loki | 内部 9080 | 256M | promtail_positions volume |
| **aats-jaeger** | 1.57 | 分布式链路追踪 | 127.0.0.1:16686 / 4317 / 4318 | 1536M | jaeger_badger_data volume |
| **aats-prometheus** | 2.51.0 | AATS 进程指标存储 | 127.0.0.1:9090 | 256M | prometheus_data volume |
| **aats-grafana** | 10.4.4 | 4 数据源统一看板 + 5 条告警 | 127.0.0.1:3000 | 512M | grafana_data volume |

基础设施合计约 **7.2 GB** 内存（9 个服务）。所有服务加入 `aats` bridge 网络。

### 应用层：`docker-compose.aats.yml` + Profile 叠加

| 容器 | 进程角色 | 入口 | 健康检查方式 | 内存上限 |
|------|----------|------|-------------|----------|
| **aats-gateway** | gateway | uvicorn FastAPI | `curl /healthz` | 1024M |
| **aats-market** | market | `python -m apps.market_gateway.main` | 心跳文件 mtime | 1024M |
| **aats-decision** | decision | `python -m apps.decision_engine.main` | 心跳文件 mtime | 1024M |
| **aats-execution** | execution | `python -m apps.execution_engine.main` | 心跳文件 mtime | 1536M |
| **aats-rdp-daemon** | rdp-daemon | `python scripts/rdp_task_daemon.py` | 存活文件 | 1024M |

Profile 叠加文件（如 `docker-compose.aats.derivatives-live.yml`）只做两件事：注入 `env_file`（凭证）和 `AATS_PROFILE` 环境变量。

Monolith 兜底 profile（`docker-compose.aats.derivatives-live-monolith.yml`）：只启 gateway 容器，其余 3 个服务 `replicas: 0`。

---

## 六、进程启动引导链

```
docker compose up -d
  |
  v
tini (PID 1, 信号转发)
  |
  v
scripts/compose_entrypoint.py
  |  读取 AATS_PROFILE -> 注入 AATS_STARTUP_PROFILE / AATS_ENV_TEMPLATE_PROFILE
  |  os.execvp() 替换当前进程
  |
  v
apps/<role>/main.py
  |
  v
aats.bootstrap.process_lifecycle.run_process_sync()
  |  load_settings() -> configure_logging() -> build_runtime(process_role=...)
  |
  |  build_runtime 内部按角色切片：
  |  |- _build_shared_slice()     所有进程共享（EventBus, NATS, Redis, MarketGateway）
  |  |- _build_market_slice()     仅 market（OKX WS, FeatureEngine, 发布循环）
  |  |- _build_decision_slice()   仅 decision（策略引擎, 审计, AI评估）
  |  +- _build_execution_slice()  仅 execution（下单, 持仓, 对账）
  |
  |  start_background_tasks()
  |  install_shutdown_signals()   注册 SIGTERM / SIGINT
  |  启动心跳 _heartbeat_loop()  每 5s touch /tmp/aats_<role>_heartbeat
  |
  v
  await stop_event.wait()  <-- 等待信号
  |
  v
  stop_background_tasks()  -> 心跳最后停（确保 docker 探到干净退出）
```

### compose_entrypoint.py 的作用

`docker-compose --env-file` 注入的环境变量不会触发 AATS 的 `load_profiled_dotenv_into_process()`，因此需要一个 shim：

1. 读取 `AATS_PROFILE` 环境变量
2. 映射到 `PROFILE_STARTUP_PROFILES` dict，注入派生变量
3. `os.execvp()` 替换当前进程为实际业务命令（保留环境）

---

## 七、健康检查机制

### Gateway（HTTP 轻量探活）

```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -fs http://localhost:${AATS_API_PORT}/healthz"]
  interval: 15s
  timeout: 5s
  retries: 5
  start_period: 30s
```

`/healthz` 只要 FastAPI lifespan 完成就返回 200，不依赖任何下游服务。

### Daemon 进程（心跳文件 mtime）

```yaml
healthcheck:
  test: ["CMD-SHELL", "test -f /tmp/aats_<role>_heartbeat && test $(($(date +%s) - $(stat -c %Y /tmp/aats_<role>_heartbeat))) -lt 30"]
  interval: 15s
  timeout: 5s
  retries: 5
  start_period: 30s
```

`process_lifecycle.py` 每 5s touch 文件，Docker 检查 mtime 与当前时间差 < 30s。

**关键设计**：心跳 task 独立于业务 background_tasks，`stop_background_tasks()` 期间心跳仍在打，docker 看到的是干净退出而非 unhealthy。

### 深度健康检查

`/system/health` 端点提供完整诊断（market 连接、账户快照、kill switch、组件 blocker），但不用于 Docker healthcheck 以避免误判。

---

## 八、环境隔离体系

```
.env.derivatives.live           凭证 + 风控参数（gitignored）
    |
    v
compose_entrypoint.py           AATS_PROFILE -> 注入衍生变量
    |
    v
load_profiled_dotenv()          profile_resolution 加载 .env.template.<profile>
    |
    v
AATSSettings                    Pydantic 校验 + 类型安全
    |
    v
build_runtime(process_role)     按角色切片实例化服务
```

每个 profile 独享：数据库、API 端口、交易对、风控参数、凭证，互不干扰。

### Profile 环境文件内容

每个 `.env.<profile>` 包含：

- 交易对与账户：`AATS_DEFAULT_SYMBOL`、`AATS_ALLOWED_SYMBOLS`、`AATS_INITIAL_USDT_BALANCE`
- 数据库：`AATS_DATABASE_URL`、`AATS_DB_NAME`、`AATS_DATABASE_RUNTIME_LOCK_KEY`
- API 端口：`AATS_API_PORT`
- 交易所凭证：`AATS_OKX_API_KEY`、`AATS_OKX_API_SECRET`、`AATS_OKX_API_PASSPHRASE`
- 风控限制：仓位上限、杠杆、保证金阈值
- 会话安全：`AATS_OPERATOR_SESSION_SECRET`、`AATS_OPERATOR_SESSION_COOKIE_NAME`
- 功能开关：`AATS_ACTIVE_PARAMETERS_ENABLED`（RDP 治理层）

---

## 九、数据库初始化与备份

### 初始化

`deploy/wsl2-dev/initdb/create-databases.sh` 在 Postgres 容器首次启动时自动创建 5 个隔离数据库：

| 数据库 | 用途 |
|--------|------|
| `aats_spot` | 现货模拟 |
| `aats_derivatives` | 衍生品模拟 |
| `aats_live_spot` | 现货实盘 |
| `aats_live_derivatives` | 衍生品实盘 |
| `aats_research` | RDP 治理层 |

### 备份

`deploy/wsl2-dev/scripts/backup_postgres.sh`：

- 使用 `docker exec aats-postgres pg_dump -F c`（自定义格式）
- 输出：`backups/wsl2-postgres/aats_<YYYYMMDDTHHMMSS>.dump`
- 自动删除超过 14 天的旧备份（`RETENTION_DAYS` 可配）

推荐 cron（每 30 分钟）：

```bash
*/30 * * * * cd ~/aats && ./deploy/wsl2-dev/scripts/backup_postgres.sh >> logs/backup.log 2>&1
```

### 恢复

`deploy/wsl2-dev/scripts/restore_postgres.sh`：

```bash
docker exec -i aats-postgres pg_restore \
  -U "$DB_USER" -d "$DB_NAME" -F c --clean --if-exists < backup.dump
```

---

## 十、运维监控

### 定时巡检（`deploy/wsl2-dev/scripts/cron_healthcheck.sh`）

每小时巡检以下内容：

- cron 服务状态（`systemctl is-active cron`）
- 基础设施容器状态（postgres / nats / redis）
- RDP 任务日志时效性：
  - `data_maintenance.log`（最大 25h 间隔）
  - `governance_cycle.log`（最大 25h）
  - `research_cycle.log`（约 7.5 天）
  - `backup.log`（最大 40 分钟）
- 最新 workflow 运行报告
- 备份数量与总大小

### Grafana 统一看板

自动 provision 数据源：

- **Loki** -> 集中日志查询
- **Jaeger** -> 分布式链路追踪
- **Postgres** -> 业务指标

访问地址：`http://127.0.0.1:3000`

### RDP 任务守护进程

`scripts/rdp_task_daemon.py`：

1. 轮询 `governance.rdp_task_queue` 表（默认 10s 间隔）
2. 领取待处理任务（`SELECT FOR UPDATE + UPDATE running`）
3. 执行对应 workflow 脚本
4. 更新任务状态（done / failed）、退出码、错误信息
5. 写 `/tmp/rdp_daemon_alive` 供 healthcheck 使用

工作流超时配置：

| 工作流 | 超时 |
|--------|------|
| `data_maintenance` | 900s (15 min) |
| `research_cycle` | 3600s (60 min) |
| 默认 | 1800s (30 min) |

---

## 十一、典型发布操作序列

### 场景 A：常规代码修改后部署

```bash
./scripts/deploy.sh --profile derivatives-live --commit "fix exit policy sync"
# 自动完成：commit -> sync -> build -> restart -> healthcheck
```

### 场景 B：手动分步部署

```bash
# 1. Windows 侧提交
git add -A && git commit -m "fix exit policy sync"

# 2. 同步到 WSL2
./scripts/sync_to_wsl2.sh pull

# 3. WSL2 内构建 + 启动
cd ~/aats/deploy/wsl2-dev
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 \
  -f docker-compose.aats.derivatives-live.yml \
  --env-file ../../.env.derivatives.live build

docker compose -f docker-compose.aats.yml --env-file .env.wsl2 \
  -f docker-compose.aats.derivatives-live.yml \
  --env-file ../../.env.derivatives.live up -d

# 4. 验证
curl http://127.0.0.1:8011/healthz               # 轻量探活
curl http://127.0.0.1:8011/system/health | jq .   # 完整诊断
docker compose -f docker-compose.aats.yml ps       # 容器状态
```

### 场景 C：打稳定版标签

```bash
git tag -a stable-v3 -m "Stable release v3: exit policy sync fix"
git push origin main --tags
```

### 场景 D：紧急回滚到单进程模式

```bash
./scripts/deploy.sh --profile derivatives-live-monolith
# monolith overlay: 只启 gateway 容器，其余 3 个 replicas: 0
```

---

## 十二、关键设计决策

| 决策 | 原因 |
|------|------|
| git sync 而非 rsync | 保留 git 历史，只同步已 commit 状态 |
| 单镜像 + 多角色 | 减少 CI 产物，简化版本一致性 |
| tini 做 PID 1 | 正确转发 SIGTERM，避免僵尸进程 |
| 心跳文件而非 HTTP | daemon 无 HTTP listener，mtime 检测挂死 vs 崩溃 |
| 心跳独立于业务 task | 确保优雅停机期间 docker 不误判 unhealthy |
| compose_entrypoint.py shim | 桥接 docker-compose env 注入与 AATS managed profile 系统 |
| monolith 兜底 profile | 4 进程出问题时可快速切回单进程不停服 |
| 5 个隔离数据库 | 现货 / 衍生品 x 模拟 / 实盘完全隔离，避免数据串扰 |

---

## 十三、关键文件索引

### 部署脚本

| 文件 | 用途 |
|------|------|
| `scripts/deploy.sh` | 一键部署管线 |
| `scripts/sync_to_wsl2.sh` | Windows 与 WSL2 代码同步 |
| `scripts/compose_entrypoint.py` | Profile 环境变量注入 shim |
| `scripts/wsl_sudo.sh` | WSL2 sudo 安全包装 |

### Docker 配置

| 文件 | 用途 |
|------|------|
| `deploy/wsl2-dev/Dockerfile` | 多阶段 Python 3.12 镜像 |
| `deploy/wsl2-dev/docker-compose.yml` | 基础设施（PG/Redis/NATS/Loki/Jaeger/Grafana） |
| `deploy/wsl2-dev/docker-compose.aats.yml` | 4 进程应用 + RDP daemon |
| `deploy/wsl2-dev/docker-compose.aats.*.yml` | Profile 叠加（凭证 + 环境标识） |
| `deploy/wsl2-dev/.env.wsl2.template` | 基础设施凭证模板 |

### 数据库与备份

| 文件 | 用途 |
|------|------|
| `deploy/wsl2-dev/initdb/create-databases.sh` | 首次启动创建 5 个数据库 |
| `deploy/wsl2-dev/scripts/backup_postgres.sh` | 自动备份 + 14 天轮转 |
| `deploy/wsl2-dev/scripts/restore_postgres.sh` | 从备份恢复 |

### 监控与运维

| 文件 | 用途 |
|------|------|
| `deploy/wsl2-dev/scripts/cron_healthcheck.sh` | 每小时基础设施 + RDP 巡检 |
| `scripts/rdp_task_daemon.py` | RDP 任务队列守护进程 |

### 进程入口

| 文件 | 角色 |
|------|------|
| `apps/api_gateway/main.py` | gateway（FastAPI + lifespan） |
| `apps/market_gateway/main.py` | market（行情接入） |
| `apps/decision_engine/main.py` | decision（策略决策） |
| `apps/execution_engine/main.py` | execution（订单执行） |

### 运维文档

| 文件 | 内容 |
|------|------|
| `docs/operations/platform_runbook.md` | 研究平台阶段与日常运维 |
| `docs/operations/wsl2_sync_workflow.md` | Windows 与 WSL2 同步流程 |
| `docs/operations/stage7_wsl2_realrun_runbook.md` | Docker Compose 真实启动记录与排障 |
| `docs/operations/operator_checklist.md` | 上线前后检查清单与故障恢复 |
| `docs/operations/reliability_alerting.md` | 监控与告警配置 |
