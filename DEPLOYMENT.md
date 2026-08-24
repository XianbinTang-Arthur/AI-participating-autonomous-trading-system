# AATS 部署与运行说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。


最后核对：2026-08-23（代码基线 `be9179e`）
适用范围：Windows 本地启动、WSL2 标准部署、profile 选择、启动/停机、健康检查

部署的目的不是“把系统跑起来”本身，而是为长期稳定盈利提供可靠运行底座。任何部署、启动、切换 profile 或放开 live submit 的动作，都必须服务于 AI 资本的稳健积累目标，并且优先满足风控、恢复、审计、治理和 fail-closed 要求。完整定位见 [docs/project_positioning.md](docs/project_positioning.md)。

## 1. 部署拓扑

```text
Windows workspace
  D:\文件\project\AIParticipatingAutonomousTradingSystem
        │
        │ scripts/deploy.sh：提交（可选）/同步/构建/启动/健康检查
        ▼
WSL2 Docker
  ├─ Postgres
  ├─ Redis
  ├─ NATS JetStream
  ├─ Loki / Promtail
  ├─ Jaeger
  ├─ Prometheus / Grafana / Redis Exporter
  └─ AATS application containers
       ├─ gateway
       ├─ market
       ├─ decision
       ├─ execution
       ├─ rdp-daemon
       └─ derivatives-live 额外：liquidations-daemon / microstructure-collector
```

`deploy/wsl2-dev/` 是本地开发/演练栈，不是生产级 HA、安全或灾备模板。

标准部署唯一入口是 `bash scripts/deploy.sh ...`。Compose 文件是该脚本的内部实现，不是第二套人工部署入口；不要直接运行 `docker compose up/down/restart`，也不要用 `rsync` 同步代码。

## 2. Profile 与端口

| Deploy profile | AATS profile | 默认用途 | API port | DB |
| --- | --- | --- | --- | --- |
| `spot` | `spot` | 现货模拟/联调 | 8000 | `aats_spot` |
| `spot-live` | `spot_live` | 现货受保护 live | 8010 | `aats_live_spot` |
| `derivatives` | `derivatives` | 合约模拟/联调 | 8001 | `aats_derivatives` |
| `derivatives-live` | `derivatives_live` | 合约受保护 live | 8011 | `aats_live_derivatives` |
| `derivatives-live-monolith` | `derivatives_live` | 合约 live 单进程兜底 | 8011 | `aats_live_derivatives` |

## 3. live 启动硬门槛

live exchange-coupled runtime 必须满足：

| 项 | 必须状态 |
| --- | --- |
| execution backend | `okx` |
| account backend | `okx` |
| account read | enabled |
| storage | `postgres` |
| database URL | 已配置 |
| database runtime guard | enabled |
| OKX credentials | 已配置 |
| Operator auth | enabled |
| unsafe unauthenticated write | disabled |
| session cookie | live 环境必须 secure |

任何一项不满足，runtime 应 fail closed。

这里的原则不是尽快上线，而是只允许具备稳定盈利验证基础、真实净收益可归因能力和完整安全约束的运行时进入 live。

## 4. 标准部署入口

```bash
# 从 Windows 仓库根目录执行；代码已经提交时使用：
bash scripts/deploy.sh --skip-commit

# 指定非默认 profile：
bash scripts/deploy.sh --profile derivatives --skip-commit
```

脚本的实际序列是：精确提交（可跳过）→ 同步到 WSL2 native checkout → 停止旧栈 → 构建镜像 → 清理悬空镜像 → 启动基础设施 → 启动应用 → 健康检查与报告。默认 profile 是 `derivatives-live`。

前置文件只说明位置，不在文档或日志中展示内容：

- WSL2 checkout 根目录：`.env.wsl2`（旧位置 `deploy/wsl2-dev/.env.wsl2` 仅兼容迁移）。
- Windows 与 WSL2 checkout 根目录：所选 profile 的 `.env.*`。
- live profile 需要 WSL2 中可用的 OpenSSL；部署脚本会在运行目录生成本地 Operator TLS 证书。

## 5. 应用启动

### 5.1 本地 PowerShell 单进程

```powershell
.\.venv\Scripts\python.exe scripts\start_api.py --profile derivatives --host 127.0.0.1 --port 8011
```

该命令显式覆盖为 `http://127.0.0.1:8011`。不传 `--port` 时使用 profile 的有效端口（仓库模板中 `derivatives` 为 `8001`）。`start_api.py` 当前不配置 TLS，不用于 live 浏览器会话。

### 5.2 WSL2 标准多进程

```bash
bash scripts/deploy.sh --profile derivatives-live --skip-commit
```

### 5.3 Docker 单进程兜底

```bash
bash scripts/deploy.sh --profile derivatives-live-monolith --skip-commit
```

## 6. 启动后必须检查

| 检查 | 命令/位置 | 通过标准 |
| --- | --- | --- |
| gateway liveness | `GET /healthz` | 200，只表示 FastAPI 活着 |
| system health | `GET /system/health` 或 UI | 无 critical blocker |
| process roles | deployment report / container health | gateway/market/decision/execution/rdp-daemon 都启动 |
| live overlay collectors | container health + freshness/heartbeat | derivatives-live 下 liquidations-daemon 与 microstructure-collector 均健康且数据新鲜 |
| DB runtime lock | Postgres `pg_locks` | 每个 role 一把独立 advisory lock |
| NATS | `curl :8222/healthz` | healthy |
| Redis | `redis-cli ping` | PONG |
| reconciliation | Operator UI / reports | 无 unresolved high/critical finding |
| kill switch | Operator UI | live 前必须明确状态 |
| stuck commands | recovery status | 无 stale/stuck submit 未处理 |
| active parameters | Operator/RDP view | 版本、actor、gate 可追踪 |

注意：`/healthz` 不是 trading ready gate。live 前必须看 `/system/health`、reconciliation、account freshness、kill switch 和 recovery status。

当前 `deploy.sh` 的自动健康门只等待 gateway/market/decision/execution/rdp-daemon；它尚未把两个 derivatives-live collector 纳入 required list。因此部署脚本成功不代表这两个 collector 已验证，必须单独检查其容器健康和数据 freshness。

## 7. 安全启动顺序

1. 启动 Postgres、Redis、NATS、可观测性基础设施。
2. 启动 gateway。
3. 启动 market，确认行情 freshness。
4. 启动 decision，但保持 live submit 受控。
5. 启动 execution，确认 account snapshot freshness。
6. 检查 recovery status、stuck commands、reconciliation。
7. 人工确认 kill switch 和 Operator auth。
8. 仅在所有检查通过后允许 live submit。

## 8. 安全停机顺序

先运行只读预检（默认 dry-run）：

```bash
bash scripts/ops/safe_shutdown.sh --reason "planned_maintenance"
```

确认预检输出后才执行：

```bash
bash scripts/ops/safe_shutdown.sh --apply --confirm --reason "planned_maintenance"
```

脚本按 `execution → decision → rdp/collectors → market → gateway` 停应用，再处理基础设施，并写入 `artifacts/shutdown_snapshots/`。有 open orders/positions 时默认拒绝继续；`--force-with-money` 和 `--skip-preflight` 都是高风险紧急通道，不属于常规停机流程。

核心原因：

- 先停止新决策，避免继续产生 order intent。
- 再停止 execution，避免处理中途继续扩展执行链。
- market/gateway 最后停，方便观察状态。

停机前必须确认：

- 没有未处理 `PENDING` submit/cancel command。
- 没有 ambiguous stale `SENT` submit。
- 没有未结算 fill。
- open orders 和本地 order state 已对齐或明确进入人工处理。

## 9. 参数发布部署要求

active parameter change 是生产变更，不是普通研究脚本执行。

生产 apply 必须具备：

- approved recommendation。
- pre-apply gate result。
- release id。
- actor。
- observation plan。
- rollback plan。
- apply history。

生产环境不得跳过 gate。

## 10. 备份与恢复

Postgres 手动备份（在 WSL2 checkout 中执行；不会展示凭证）：

```bash
cd ~/aats/deploy/wsl2-dev
RETENTION_DAYS=14 ./scripts/backup_postgres.sh
```

恢复演练：

```bash
./scripts/restore_postgres.sh latest
```

恢复前必须：

1. 记录当前 git commit 和 profile。
2. 备份当前数据库。
3. 停止 AATS 应用进程。
4. 恢复后启动 monolith 或 execution recovery 做一致性检查。

## 11. 观测入口

| 系统 | URL |
| --- | --- |
| Gateway UI | dev profile：`http://127.0.0.1:<port>/ui`；标准 live 部署：`https://127.0.0.1:<port>/ui` |
| Grafana | `http://127.0.0.1:3000` |
| Jaeger | `http://127.0.0.1:16686` |
| Prometheus | `http://127.0.0.1:9090` |
| NATS monitoring | `http://127.0.0.1:8222` |
| Loki | `http://127.0.0.1:3100` |

关键日志事件：

- `order_intent_received`
- `order_intent_blocked`
- `order_submit_failed`
- `fill_event_created`
- `fill_outcome_persist_failed`
- `reconciliation_*`
- `phase4_stuck_sent_submit_commands`
- `kill_switch_*`

## 12. 上线前最小测试

```powershell
.\.venv\Scripts\python.exe -m ruff check aats/ --fix
.\.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
wsl -d Ubuntu bash -c "cd ~/aats && source ~/aats-venv/bin/activate && pytest tests/integration/test_execution_outbox_postgres.py tests/integration/test_phase4_recovery_reconciliation_runtime.py -x -q"
```

集成测试必须在 WSL2 中运行。根据实际变更范围补充更窄的集成测试和回归测试；测试分层、模拟 profile 和证据模板见 [`docs/testing/README.md`](docs/testing/README.md)。
