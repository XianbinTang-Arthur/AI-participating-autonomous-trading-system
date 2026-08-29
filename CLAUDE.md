# CLAUDE.md — AATS 项目操作手册

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。

> 文档状态：现行约束。最后核对：2026-08-28（起始 HEAD `82e600842a7ef360ab63c103c6dea1eae2267898`；包含当前 FS-016 readiness lease 重启安全候选，以本文档所在最终 HEAD 为准）。运行架构、部署、安全纪律或文档治理变化时必须同步复核。

> 本文件是仓库级操作手册。所有在本仓库工作的编码代理必须遵守此文档约束。

## 项目概述

AATS（AI Participating Autonomous Trading System）— 一个以 AI 为核心受益主体、真金白银运行的加密货币自动交易系统。
AATS 的首要意义是通过自动化交易持续追求长期稳定盈利，为 AI 的长期自主发展积累资本；所有架构、研究、风控、执行、恢复和运维工作都应服从这个目标。
代码当前集成的交易所是 OKX，managed profile 同时覆盖现货与衍生品；仓库的主要生产路径面向衍生品。实际运行中的交易所、账户模式和交易品种属于现场事实，必须在操作当时验证，不能由本文档推断。

## 架构

四个主交易切片，外加 RDP/采集守护进程，Docker 容器部署在 WSL2 中：

| 进程 | 容器名 | 职责 |
|------|--------|------|
| gateway | aats-gateway | API 网关 + 前端 |
| market | aats-market | 行情数据 |
| decision | aats-decision | 决策引擎 |
| execution | aats-execution | 执行引擎 + OKX 适配器 |
| rdp-daemon | aats-rdp-daemon | Research Data Platform |
| liquidations-daemon | aats-liquidations-daemon | derivatives 模拟公共强平数据采集；future live 拓扑也声明该角色 |
| microstructure-collector | aats-microstructure-collector | derivatives 模拟公共 BBO/books5/trades/OI 采集；future live 拓扑也声明该角色 |

基础设施容器：aats-postgres, aats-redis, aats-redis-exporter, aats-nats, aats-jaeger, aats-grafana, aats-prometheus, aats-loki, aats-promtail

“四进程”只表示主交易 role 数量。`derivatives` 模拟 Compose 当前共有 7 个应用容器，并把两个公共 collector 纳入部署健康与 heartbeat 证据；静态声明不证明现场数据新鲜。future `derivatives-live` 也声明 7 个角色，但标准入口硬禁用全部 live profile。

## 关键路径和文件

### 部署

- **部署脚本**: `scripts/deploy.sh` — profile 必填；8 个编号阶段（提交→同步→先构建→停止→清理→基础设施→显式 schema migration/校验→应用）之后执行健康检查、写入模拟证据包并输出非生产报告
- **WSL2 同步**: `scripts/sync_to_wsl2.sh` — 必须用这个同步代码，**绝对不要用 rsync**
- **Compose 文件目录**: `deploy/wsl2-dev/`
- **主 Dockerfile**: `deploy/wsl2-dev/Dockerfile`

### 配置文件

- **衍生品实盘环境变量**: `.env.derivatives.live`（位于项目根目录，数据库连接在第 19 行）
- **WSL2 环境变量**: `.env.wsl2`（Postgres 密码等基础设施凭证）
- **配置模板**: `configs/templates/`（仅供参考，不是运行时配置）
- **运行时参数**: managed profile 代码基线 + `configs/strategy_profiles/<profile>.yaml` + 允许的 `.env.*` override；RDP active parameters 最后从 Postgres `governance.active_parameter_sets` 注入
- **Managed 配置真实性**: Phase 3P 后 strategy YAML 必须是 mapping，runtime defaults 与 YAML 的每个 key 都必须属于 `AATSSettings.model_fields`，未知 key 在 runtime 构建前失败；`strategy_profile_auto_rollback_enabled` 从未有消费者且已删除，不能写成已实现能力
- **Profile recommendation 边界**: `approve/release` 只推进研究治理状态；Phase 3M 后 `profile-recommendations/{id}/apply` 与 `/rollback` 均在授权检查后无写入 `501`。在 execution-owned generation/worker readback 完成前，不得把它们用于运行参数变更
- **回测成交证据边界**: 当前只接受 `next_bar_event_v2` + `ohlcv_participation_cap_contract_v3` + `backtest-run/v2` manifest 的新 SPOT 回测作为 bar-proxy 候选；CLI 必须提供完整 `InstrumentContract`、显式 `base|quote` 买入手续费资产并写入全新输出目录，custom adapter 必须声明非空 `algorithm_version` 和精确 `accepted_parameter_keys`。严格 v2 以持久化 execution basis 重放 `FillSimulator`，再按逐点 mark/仓位账本重放 `PositionTracker`，闭合 partial/no-fill、fee/slippage、base-fee dust、PnL、累计费和净值；bar-end decision time、参数类型、tick/lot/min 也失败关闭。`backtest-evidence-scorecard/v2` 必须有显式 fill attribution、零 cadence gap 才可进入 Route-A scaffold；bundle v2 的单个 daily snapshot 固定标为观察窗未完成。旧 v2 fill、无 manifest/无版本 replay 产物只可审计/校准。三类订单仍仅是 OHLCV proxy；reference/mark 的来源真实性仍受未封存 Gold 限制，当前风险指标是无初始本金的 bar PnL-increment proxy，不是资本收益率 Sharpe。derivative/MARGIN、funding、source-aware contract snapshot、初始资本/报告币种与 L2 calibration 均失败关闭或 OPEN，不能外推 live 容量或收益
- **收益证据 v1 边界**: 公共微观结构 eligibility、v2 候选审计/复跑、统计门、L2 event replay、paper calibration、一次性 holdout 账本、参数 generation、故障/readiness schema 的现行入口见 `docs/operations/profit_readiness_runbook.md`。格式 v1 只能产生模拟就绪，`production_ready`/`trading_ready` 固定 false；runtime worker 尚未接 ACK，profile apply/rollback 继续 501
- **Dashboard 无障碍边界**: Phase 3O 后详情抽屉必须保持原生 modal dialog、初始/返回焦点、Escape/backdrop 统一关闭和 reduced-motion CSS/JS 契约；静态/单元通过不能替代目标浏览器、键盘、NVDA/VoiceOver 与 axe 实测

### 数据库

- **Postgres 容器名**: `aats-postgres`
- **Postgres 用户**: `admin`
- **基础设施默认数据库**: `aats`；实际交易 profile 使用各自的独立数据库
- **衍生品实盘数据库**: `aats_live_derivatives`
- **RDP 数据库**: `RDP_DATABASE_URL`；容器未显式设置时可复用 `AATS_ACTIVE_PARAMETER_DB_URL`。绝不读取或展示凭证文件内容
- **数据库命名规律**: `aats_live_derivatives`（不是 `aats_derivatives_live`）
- **连接预算**: `aats/storage/connection_budget.py` 是 SQLAlchemy pool ceiling 的单一真源；四进程声明拓扑上限 150，Compose 普通连接容量 197、名义余量 47。该算术不是负载或运行状态证明；目标压测、瞬时 engine、迁移/恢复/admin 和联合内存预算仍开放。

### API

- **Gateway 端口**: 当前本地衍生品模拟模板为 8001；future 衍生品实盘模板 8011 仍保留但启动禁用，查看 `AATS_API_PORT`
- **Gateway 宿主绑定**: WSL2 Compose 固定 `127.0.0.1`；本地 `start_api.py` 只接受模拟 profile 和 loopback host
- **Gateway 浏览器边界**: 常规、HTTPException/认证失败与 Host 400 响应由最外层 user middleware 覆盖固定 CSP/frame/nosniff/referrer/permissions/COOP/CORP 安全头；Host 仅允许 `127.0.0.1`/`localhost`/`::1`/`testserver`；HSTS 仅在实际 HTTPS scope 下输出；框架最外层未捕获 500 仍须故障注入
- **认证方式**: Session-based（需先登录获取 cookie）
- **登录资源边界**: 同步 DB/PBKDF2/审计完整移入有界 worker；每 Gateway 进程默认并发 4、排队 1 秒，60 秒窗口默认 global/client/identity 为 60/20/10；client 只取 ASGI socket，不信任 `X-Forwarded-For`。这是每进程代码保护，分布式限流和目标负载仍 OPEN
- **健康检查**: `GET /healthz`；关键 task 结束或纳管固定周期 task 成功进度超时会 503，但事件驱动 task/跨进程交易 readiness 仍须另验
- **系统恢复**: `POST /system/resume`

## 常用部署命令

```bash
# 标准本地模拟部署（已提交代码后）
bash scripts/deploy.sh --profile derivatives --skip-commit

# 带自动提交的部署
bash scripts/deploy.sh --profile derivatives --commit "修复描述"

# 无缓存重建
bash scripts/deploy.sh --profile derivatives --no-cache --skip-commit

# 当前真实资金 NO-GO：所有 live profile 在同步/构建/停服/迁移前非零退出，--yes 不能绕过
```

标准模拟 deploy 会在 sync 后自动生成非秘密 runtime readiness generation，注入四主进程并记入模拟证据包。不要把该值写进 `.env.*` 或手工复用到新部署；直接 Compose 缺代次时失败是预期安全语义。严格 NATS/hybrid 四主进程的现行候选使用 Redis-only protocol v2 和全局 `aats:runtime:owner:<role>` key；generation 只存在 payload 和 peer barrier。每个 role 在任何 NATS I/O 前 claim `PROVISIONING`，立即开始续租并启动独立 subprocess watchdog，然后完成 55 秒 takeover quarantine；build 完成后 CAS 为 `READY`。父子进程统一使用 POSIX `CLOCK_BOOTTIME` / Windows `GetTickCount64`，并以 POSIX pidfd / Windows process creation FILETIME 固定父进程身份。所有 peer `READY` 后先 flush build 期 publish 缓冲（最多 4,096 条且 64 MiB），再开放 callback 与 background。`NatsDeliveryGate` 在未 ready 或 `ABORT` 时不 parse/persist/handler/ack/nak；仅 strict 四主进程 NATS/hybrid 注入 gate 并把 push consumer 的 `max_ack_pending` 固定为 `1`。non-strict、in-memory 与 monolith 不注入该 gate，也不强制窗口为 `1`。checker/runtime 共用 mutable migration policy：只允许 snapshot/transient 正数旧 `ack_wait` 向声明目标提高；event `ack_wait`、任意 `max_deliver` 和不安全非事件 drift 失败关闭。critical ALL durable 不删除 cursor。现存 durable 在 update/bind 前冻结 created、真实 inbox 与四维 cursor，回读/post-bind/READY/steady supervision 均拒绝同名重建、inbox 变化或 cursor 回退；实际 inbox 不得写入日志/证据。flow control/idle heartbeat 历史差异当前仅保留并告警，仍是 OPEN 运行边界。

owner TTL 为 60 秒，每 10 秒续租，安全停机 margin 30 秒，hard-exit grace 10 秒。每次成功写/续租 `PROVISIONING` lease 后，本地 hard fence 最多滑动到该次写入后 50 秒；这不是 claim 后 50 秒总上界。claim 到 `READY` promotion 另有不可被续租延长的 180 秒绝对上界，第 170 秒冻结续租并进入最后 10 秒 fatal grace；确定失租零宽限。任一关键故障先冻结续租并进入不可 disarm 的 fatal deadline；正常停机同样先冻结续租，以 10 秒硬截止完成业务/NATS cleanup，随后才 disarm watchdog、owner-aware delete。Redis 使用 `noeviction`，写满必须显式失败；NATS 连续断连 30 秒会进入 critical failure。默认应用健康预算为 210 秒。不得手工写 owner key，也不得以手工 Compose/容器重启冒充受控矩阵。

标准部署在任何 mutation 前取得并持续持有长寿命 WSL `flock`。生产锁路径固定为 `/tmp/aats-standard-deploy.lock`；只有 `AATS_DEPLOY_TEST_MODE=true` 的隔离测试才允许通过 `AATS_DEPLOY_LOCK_FILE` 改路径，生产覆盖直接失败。Windows 端每 3 秒更新 lease，WSL holder 在 12 秒失联后释放锁；新 holder 取得 flock 后若发现其他仍新鲜的 predecessor lease，会由 holder 持续刷新自己的 lease 并隔离等待，直至前任 lease 清除或过期才报告取得锁。每个外部步骤 spawn 前复核 holder/heartbeat/flock，spawn 后立即全局登记 active child PID/context；活动中丢锁会终止并 wait 进程树。`TERM/HUP/INT/EXIT` 必须先终止并 wait active child，再按 heartbeat、lease、holder fd/flock 的顺序释放，禁止先放锁后遗留 mutation。cutover 会停止所有已知 profile 的七个应用容器，只接受 `exited/dead` 或明确 not-found，记录并前后比较容器 ID、状态、`StartedAt`、`FinishedAt`、`RestartCount`，同时查询 preflight 精确时间窗内的 Docker lifecycle events。第一次只读 preflight 位于基础设施-only up 后、full-down 前；full-down 后重建 quiescence 基线，正常 infra/schema 完成后、app up 前再执行第二次。schema v3 使用人工 authoritative ownership manifest，并由动态 assembly 测试证明与四个 `build_runtime()` 精确一致：`77` 个 durable，角色 `31/8/27/11`，语义 `49/24/4`。preserved install 要求 exact `77` 且无 unowned；fresh install preflight 要求为空，app-up 后 final 才要求 exact `77`。任何 outstanding/config drift/查询失败或 quiescence 变化都必须保留 NATS 在线并阻断。v3 PASS 绑定 lock id、generation、部署提交与 quiescence，最终证据重算 continuity、记录 path/hash，并封存两次 no-secret canonical durable projection/hash。protocol v1 -> v2 首次发布和回滚禁止 rolling/mixed version；标准 stop 本身不是 drain 证明，BLOCKED outstanding 只能在人工批准变更窗用匹配旧消费者自然 drain 后重跑，禁止自动 ACK/delete/update/recreate/reset/purge。LAST/NEW 的 ACK-window backlog 与 immutable-drift 重建是两个声明丢弃语义分支；标准 preflight 会先阻断 immutable drift，full-down 是额外发布门禁，不是运行时信号。当前 v3 代码已提交并通过静态/单元审查，标准入口已生成真实 v3 `BLOCKED` artifact；两次 PASS chain、app-up 与最终 evidence 仍未形成，完整故障矩阵保持 `OPEN`。

当前运行边界（2026-08-29 05:54Z）：提交 `45612697` 的标准 derivatives 流程运行 schema v3 `pre_full_down` 只读 preflight，扫描 `3` 个 stream/`78` 个 consumer，识别全部 `77` 个声明 durable、无 missing，仅因 `aats-codex_manual_resume-system_operator_command_responses` 未归属而 `BLOCKED`。七个 app 保持停止，NATS/Redis/Postgres 健康在线；流程未进入 full-down/app-up。未知 durable 未经真人 owner/release review 不得自动删除，也不得手工启动 app 绕过门禁。

该 lease/watchdog 仍不是下游执行端强制校验的单调 fencing token；Redis owner truth 与 watchdog/OS 终止同时失效的双故障尚未取得端到端排他证据。当前提交全量单测已通过；真 Redis/NATS/Docker 故障注入、标准部署 PASS、断连与受控逐角色重启矩阵仍 `OPEN`，真实资金继续 `NO-GO`；不得宣称当前提交已完成运行验收。详见 `docs/task/fs_016_runtime_readiness_lease_restart_safety_p0_sow_2026_08_28.md`。

## 开发环境

- **Windows 开发路径**: `D:\文件\project\AIParticipatingAutonomousTradingSystem\`
- **Windows Python**: `.venv\Scripts\python.exe`
- **WSL2 发行版**: Ubuntu
- **WSL2 项目路径**: `~/aats`
- **WSL2 Python venv**: `~/aats-venv`
- **集成测试**: 在 WSL2 中用 testcontainers 运行

## 运行测试

```bash
# Windows 单元测试
.venv\Scripts\python.exe -m pytest tests/unit/ -x -q

# 特定模块测试
.venv\Scripts\python.exe -m pytest tests/unit/ -k "test_name" -x -q

# WSL2 集成测试
wsl -d Ubuntu bash -c "cd ~/aats && source ~/aats-venv/bin/activate && pytest tests/integration/ -x -q"
```

## 绝对禁止

1. **绝不用 rsync 同步代码** — 会导致 WSL2 侧 git dirty state，deploy.sh 的 sync 步骤（git pull）会失败
2. **绝不读取或显示凭证文件内容** — `.env.wsl2`、`.env.*.live` 等包含密码的文件只能在进程内管道流转
3. **绝不用 `echo` 输出密码/密钥/token**
4. **绝不手动执行 `docker compose` 命令** — 使用 `scripts/deploy.sh`
5. **绝不将 `.env.*.example` 模板当作运行时配置** — 模板仅供参考
6. **绝不跳过测试声称成功** — 必须实际运行测试并报告结果
7. **重大改动前必须备份+设计+获批准** — 三步走纪律

## OrderState 持久化注意事项

OrderState 有三重持久化：
1. **Postgres 列**: `order_states.status` 和 `execution_orders.state`
2. **Postgres JSON**: `order_states.payload` 和 `execution_orders.raw_payload` 中的 `status` 字段
3. **Redis 缓存**: `aats:hot:order_state:*` keys

修改 order 状态时**三者必须同时更新**，否则系统从 JSON payload 反序列化时会读到旧状态。

## SQLAlchemy 注意事项

- 项目使用 **SQLAlchemy 2.0**
- JSON 列（非 JSONB）访问用 `.as_string()`，**不是** `.astext`（已废弃）
- JSONB 列同样统一使用 `.as_string()`；不要新增 `.astext`

## 语言和编码

- 所有前端展示文本必须使用 **UTF-8 中文**
- 与用户沟通使用 **中文**
- 代码注释和 commit message 可中英混用

## Commit 规范

- 使用语义化前缀: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`
- Commit message 简洁描述"为什么"而非"做了什么"
- 不要 `git add -A`，按文件名精确添加
