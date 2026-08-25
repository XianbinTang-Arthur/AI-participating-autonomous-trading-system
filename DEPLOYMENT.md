# AATS 部署与运行说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。


最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；包含 Phase 3A–3W 整改提交候选）
适用范围：Windows 本地启动、WSL2 标准部署、profile 选择、启动/停机、健康检查

部署的目的不是“把系统跑起来”本身，而是为长期稳定盈利提供可靠运行底座。任何部署、启动、切换 profile 或放开 live submit 的动作，都必须服务于 AI 资本的稳健积累目标，并且优先满足风控、恢复、审计、治理和 fail-closed 要求。完整定位见 [docs/project_positioning.md](docs/project_positioning.md)。

## 1. 部署拓扑

```text
Windows workspace
  D:\文件\project\AIParticipatingAutonomousTradingSystem
        │
        │ scripts/deploy.sh：提交（可选）/同步/构建/迁移/启动/健康检查
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

Phase 3T 起，Docker 的 Python 3.12 基础镜像和 Compose 的九个外部基础设施镜像均使用
`tag@sha256:digest`；Python 运行时依赖从
`requirements/runtime-py312-linux-x86_64.lock` 按 hash 安装。digest/hash 固定构建输入，
不证明镜像无漏洞或构建已成功；APT package、clean build、SBOM、CVE/license/secret 与
provenance 仍是上线前开放门禁。不得为解决 registry/PyPI 暂时失败而移除摘要或 hash。

## 2. Profile 与端口

| Deploy profile | AATS profile | 默认用途 | API port | DB |
| --- | --- | --- | --- | --- |
| `spot` | `spot` | 现货模拟/联调 | 8000 | `aats_spot` |
| `spot-live` | `spot_live` | 现货受保护 live | 8010 | `aats_live_spot` |
| `derivatives` | `derivatives` | 合约模拟/联调 | 8001 | `aats_derivatives` |
| `derivatives-live` | `derivatives_live` | 合约受保护 live | 8011 | `aats_live_derivatives` |
| `derivatives-live-monolith` | `derivatives_live` | 合约 live 单进程兜底 | 8011 | `aats_live_derivatives` |

当前标准入口只允许 `spot` 与 `derivatives`。三个 live profile 保留配置和 Compose 定义用于后续隔离验证，但在当前全系统 `REAL-MONEY PRODUCTION: NO-GO` 期间，`scripts/deploy.sh`、预热和 PowerShell wrapper 都会在任何 WSL/Docker/数据库副作用前拒绝它们，且没有 override。

无论模拟或未来 live profile，managed strategy YAML 必须是 mapping，且所有 key 必须
属于 `AATSSettings.model_fields`；未知 key 现在是启动错误，不能通过全局
`extra="ignore"` 继续。当前代码/单元契约已验证，目标容器启动证据仍需在 committed
candidate 上补齐。

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
# 从 Windows 仓库根目录执行；profile 必填，代码已经提交时使用：
bash scripts/deploy.sh --profile derivatives --skip-commit
```

脚本没有默认 profile。实际序列是：profile/live gate → 精确提交（可跳过）→ 同步到 WSL2 native checkout → **生成本次 runtime readiness generation** → **先构建新镜像** → 停止旧栈 → 清理悬空镜像 → 启动并等待全部基础设施 → **执行主交易库 + RDP 显式 schema migration job 并校验** → 启动应用 → Gateway 与全部 required container 健康检查 → 写入不覆盖的模拟部署证据包 → 报告 `simulation_stack_healthy`。down、infra up、schema、app up、health 或证据写入任一失败都会非零终止。

证据包位于 WSL checkout 的 `deploy/wsl2-dev/runtime/deployment-evidence/`，包含 commit、不可变 image ID、profile、overlay、schema job 状态、非秘密 runtime readiness generation、必需容器状态和 Gateway 实际 published binding，不含凭据。Gateway 任一实际 HostIp 不是 `127.0.0.1`/`::1` 时证据步骤失败。它固定声明 `production_ready=false`、`trading_ready=false`，不能用作上线批准。

Schema job 运行 `scripts/apply_schema_migrations.py`，主交易 root migrations 与 RDP Batch B 都用 version/checksum ledger 校验。它复用 `aats-gateway` 容器的受管 profile 环境，但覆盖命令为一次性 Python job，不启动 FastAPI 或交易后台任务。任一迁移/校验失败会使部署非零终止，应用保持停止；当前仍没有经验证的 app+schema 自动一致回滚，因此不能因这一步而声称 FS-007/009 已关闭。

前置文件只说明位置，不在文档或日志中展示内容：

- WSL2 checkout 根目录：`.env.wsl2`（旧位置 `deploy/wsl2-dev/.env.wsl2` 仅兼容迁移）。
- Windows 与 WSL2 checkout 根目录：所选 profile 的 `.env.*`。
- live profile 需要 WSL2 中可用的 OpenSSL；部署脚本会在运行目录生成本地 Operator TLS 证书。

## 5. 应用启动

### 5.1 本地 PowerShell 单进程

```powershell
.\.venv\Scripts\python.exe scripts\start_api.py --profile derivatives --host 127.0.0.1 --port 8011
```

该命令显式覆盖为 `http://127.0.0.1:8011`。不传 `--port` 时使用 profile 的有效端口
（仓库模板中 `derivatives` 为 `8001`）。`start_api.py` 当前只接受 `spot`/`derivatives`
和 loopback host，并强制使用 `monolith` 构建完整单进程 runtime；live profile、`0.0.0.0`、
`::` 与非本机地址均在 Uvicorn 启动前失败。它不配置 TLS，不用于 live 或远程浏览器会话。

Managed profile 应用启动现在只读校验 schema，不自动 `create_all`/`ALTER`。本地首次启动前必须在明确的非 live Postgres 上单独执行受控迁移；不得通过改回 `database_auto_create_schema=true` 绕过迁移账本。WSL2 模拟栈只使用上述标准 deploy 入口；live 当前不可部署。

WSL2 Compose 的 Gateway 容器内 listener 仍为 `0.0.0.0`，但宿主 published port 固定为 `127.0.0.1`。两者不是同一安全边界。需要远程访问时不得放宽 Compose 映射；应另行设计受控 proxy/VPN/mTLS，并验证目标主机防火墙、路由与证书。

标准四进程模拟部署会将同一 generation 注入应用容器。gateway/market/decision/execution 在 NATS/hybrid 路径上只接受同 generation 且 role 匹配的 Redis ready payload；Redis 写/读失败、peer 60 秒未就绪或缺失 generation 均在 publisher 前失败。Compose 对该环境变量使用 required interpolation，所以直接手工 Compose 不会默默回落到旧固定 key。本行为尚未在真实 Redis/NATS/Compose 中验证，不得视为 startup/restart PASS。

运行期另有独立的 Kill Switch permission generation：它不是部署 readiness generation。Gateway/monolith 在 peer readiness 后才启动 15 秒 Redis permission lease，execution 只读；lease task 被登记为 critical。进程关停首先停止续租并尽力删除 permission，长期 kill-switch state 保留。重新开放 live 前必须在生产等价克隆环境验证 Gateway 单向 Redis 分区、NATS 全断、kill -9、Redis TTL、execution 最终拒单、容器 restart 与告警实际时序；不得用单元测试中的 InMemory TTL 代替。

Gateway 应用层同时对 Host 失败关闭并统一输出浏览器安全头。当前固定 allowlist 只适用本机入口；未信任 Host 返回 400。HTTP 模拟入口不带 HSTS，实际 HTTPS scope 才带 `Strict-Transport-Security: max-age=31536000`。上线前必须在目标 TLS/proxy/browser 重新验证 header 未被删除或降级、CSP 无 violation，不得用单元测试替代该证据。

Operator 登录的同步 DB/KDF/审计链已移入有界 worker，并有每进程 global/client/identity
限流。默认值为并发 4、排队 1 秒、60 秒窗口 60/20/10；它们只是代码安全起点，
不是目标容量。重新开放 live 前必须在实际 Gateway worker 数、目标数据库池和受信 proxy
拓扑下验证集中限流、p95/p99、event-loop lag、拒绝率及紧急登录 SLA。不得仅通过提高
单进程限额绕过 429/503，也不得信任客户端提供的 `X-Forwarded-For`。

### 5.2 WSL2 标准模拟多进程

```bash
bash scripts/deploy.sh --profile derivatives --skip-commit
```

### 5.3 live 与单进程兜底

当前禁止通过标准入口部署 `spot-live`、`derivatives-live` 或 `derivatives-live-monolith`。没有经验证的 app+schema+parameter 一致回滚、完整 trading-readiness packet 和独立人工复核前，不得直接调用 Compose 绕过。

## 6. 启动后必须检查

| 检查 | 命令/位置 | 通过标准 |
| --- | --- | --- |
| gateway liveness / 进程内关键任务监督 | `GET /healthz` | 200 表示 FastAPI 存活且当前 supervisor 未发现关键 task 结束或纳管固定周期 task stalled；不表示 trading-ready |
| system health | `GET /system/health` 或 UI | 无 critical blocker |
| process roles | deployment report / container health | 当前 derivatives 模拟 profile 的 gateway/market/decision/execution/rdp-daemon/liquidations-daemon/microstructure-collector 都启动 |
| peer readiness generation | deployment evidence + 四主进程日志 | 同一非空 generation；all-ready 只包含同代次 role；目标启动/重启故障矩阵未跑前不标 PASS |
| schema contract | deploy schema job + root/RDP ledgers | 当前 revision 无 missing/unknown/checksum mismatch；未完成克隆库 manifest 前不外推为生产 PASS |
| public collectors | deployment evidence + Silver 最新行 | derivatives 模拟 required list 包含 liquidations-daemon 与 microstructure-collector；heartbeat 通过后仍须核对频道数据 freshness/eligibility |
| DB runtime lock | Postgres `pg_locks` | 每个 role 一把独立 advisory lock |
| NATS | `curl :8222/healthz` | healthy |
| Redis | `redis-cli ping` | PONG |
| reconciliation | Operator UI / reports | 无 unresolved high/critical finding |
| kill switch | Operator UI | live 前必须明确状态 |
| stuck commands | recovery status | 无 stale/stuck submit 未处理 |
| active parameters | Operator/RDP view | 版本、actor、gate 可追踪 |

注意：`/healthz` 不是 trading ready gate。它仍不覆盖全部事件驱动任务、整体 event-loop stall 或跨进程业务一致性；live 前必须看 `/system/health`、reconciliation、account freshness、kill switch 和 recovery status。

当前 derivatives 模拟部署成功只表示七个应用容器通过健康门。collector heartbeat 不等于四类 Silver 数据新鲜或研究窗口 eligible；必须继续生成微观结构资格、L2、paper calibration、故障矩阵和 readiness 证据。`derivatives-live` 仍在副作用前失败。`/healthz`、容器 healthy 和模拟证据包都不是 trading-ready，完整步骤见 [`docs/operations/profit_readiness_runbook.md`](docs/operations/profit_readiness_runbook.md)。

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
4. 恢复后只在隔离环境启动匹配版本的 runtime 做一致性检查；当前 live/monolith 标准发布入口禁用。

## 11. 观测入口

| 系统 | URL |
| --- | --- |
| Gateway UI | 当前 dev profile：`http://127.0.0.1:<port>/ui`；live URL 仅是未来配置，不代表可部署 |
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

Phase 3U 的数据库静态门可在 Windows 本地运行：

```powershell
.\.venv\Scripts\python.exe scripts\verify_database_connection_budget.py
```

它只校验声明 topology ceiling=150、普通容量 197、名义余量 47、engine inventory、Compose
和 CI 一致性，不连接数据库。上线前仍必须在无真实交易所写入的生产等价隔离栈压测全部
daemon/collector/RDP，覆盖慢查询、DB 短断、重连、进程重启和恢复/admin 竞争；记录 pool
wait/timeout、连接峰值、数据库内存与告警。静态通过不得替代该容量门。

```powershell
.\.venv\Scripts\python.exe -m ruff check aats/ --fix
.\.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
wsl -d Ubuntu bash -c "cd ~/aats && source ~/aats-venv/bin/activate && pytest tests/integration/test_execution_outbox_postgres.py tests/integration/test_phase4_recovery_reconciliation_runtime.py -x -q"
```

集成测试必须在 WSL2 中运行。根据实际变更范围补充更窄的集成测试和回归测试；测试分层、模拟 profile 和证据模板见 [`docs/testing/README.md`](docs/testing/README.md)。
