# AATS 部署与运行说明

最后更新：2026-04-13
适用范围：Windows/WSL2 本地部署、Docker Compose 四进程部署、profile 选择、启动/停机、健康检查

## 1. 部署拓扑

```text
Windows workspace
  D:\文件\project\AIParticipatingAutonomousTradingSystem
        │
        │ sync / docker compose / env files
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
       └─ rdp-daemon
```

`deploy/wsl2-dev/` 是本地开发/演练栈，不是生产级 HA、安全或灾备模板。

## 2. Profile 与端口

| Deploy profile | AATS profile | 默认用途 | API port | DB |
| --- | --- | --- | --- | --- |
| `spot` | `spot` | 现货模拟/联调 | 8000 | `aats_spot` |
| `spot-live` | `spot_live` | 现货受保护 live | 8001 | `aats_live_spot` |
| `derivatives` | `derivatives` | 合约模拟/联调 | 8010 | `aats_derivatives` |
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

## 4. 基础设施启动

```bash
cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev
cp .env.wsl2.template .env.wsl2
# 修改 .env.wsl2 中的 Postgres、Redis、Grafana 等密码
docker compose --env-file .env.wsl2 up -d
docker compose --env-file .env.wsl2 ps
```

健康检查：

```bash
docker compose --env-file .env.wsl2 exec postgres pg_isready -U aats
docker compose --env-file .env.wsl2 exec redis redis-cli ping
curl -s http://127.0.0.1:8222/healthz
curl -s http://127.0.0.1:3100/ready
curl -s http://127.0.0.1:9090/-/healthy
curl -s http://127.0.0.1:3000/api/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:16686/
```

## 5. 应用启动

### 5.1 本地 PowerShell 单进程

```powershell
.\.venv\Scripts\python.exe scripts\start_api.py --profile derivatives --host 127.0.0.1 --port 8011
```

### 5.2 Docker 四进程

```bash
cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev
docker compose --env-file .env.wsl2 -f docker-compose.yml up -d
docker compose --env-file .env.wsl2 -f docker-compose.aats.yml -f docker-compose.aats.derivatives-live.yml up -d
```

### 5.3 Docker 单进程兜底

```bash
docker compose --env-file .env.wsl2 \
  -f docker-compose.aats.yml \
  -f docker-compose.aats.derivatives-live-monolith.yml \
  up -d
```

## 6. 启动后必须检查

| 检查 | 命令/位置 | 通过标准 |
| --- | --- | --- |
| gateway liveness | `GET /healthz` | 200，只表示 FastAPI 活着 |
| system health | `GET /system/health` 或 UI | 无 critical blocker |
| process roles | container logs / health | gateway/market/decision/execution 都启动 |
| DB runtime lock | Postgres `pg_locks` | 每个 role 一把独立 advisory lock |
| NATS | `curl :8222/healthz` | healthy |
| Redis | `redis-cli ping` | PONG |
| reconciliation | Operator UI / reports | 无 unresolved high/critical finding |
| kill switch | Operator UI | live 前必须明确状态 |
| stuck commands | recovery status | 无 stale/stuck submit 未处理 |
| active parameters | Operator/RDP view | 版本、actor、gate 可追踪 |

注意：`/healthz` 不是 trading ready gate。live 前必须看 `/system/health`、reconciliation、account freshness、kill switch 和 recovery status。

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

推荐顺序：

```text
decision -> execution -> market -> gateway
```

原因：

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

Postgres 手动备份：

```bash
cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev
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
| Gateway UI | `http://127.0.0.1:<port>/ui` |
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
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest tests\unit -q
.\.venv\Scripts\python.exe -m pytest tests\integration\test_execution_outbox_postgres.py -q
.\.venv\Scripts\python.exe -m pytest tests\integration\test_phase4_recovery_reconciliation_runtime.py -q
```

根据实际变更范围补充更窄的集成测试和回归测试。
