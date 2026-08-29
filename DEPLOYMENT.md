# AATS 部署与运行说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。


最后核对：2026-08-29（核对基线 `main@45612697ce77399ded889d85f2c4cf365ac61746` + 05:54Z schema v3 标准 preflight 阻断证据；以本文档所在最终 HEAD 为准）
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

脚本没有默认 profile。profile/live gate 和 profile 解析之后、任何 mutation 之前，入口先取得长寿命 WSL `flock`。生产锁路径固定为 `/tmp/aats-standard-deploy.lock`；设置 `AATS_DEPLOY_LOCK_FILE` 只在同时设置 `AATS_DEPLOY_TEST_MODE=true` 的隔离测试中有效，标准部署尝试覆盖路径会非零退出。该锁由一个 WSL holder 持有同一 fd；Windows heartbeat 每 3 秒刷新独立 lease，holder 连续 12 秒未见刷新才释放 fd，且不会删除 lock inode。新 holder 即使已取得 flock，只要发现其他 `/tmp/aats-standard-deploy-lease-*` 在 12 秒内仍新鲜，就持续刷新自己的 lease 并等待，直至 predecessor lease 清除或过期才返回取得成功；这防止前一 shell/holder 异常交接时立即进入 mutation。锁贯穿预检、精确提交（可跳过）、同步、generation、构建、停启、迁移、健康检查、证据和报告；每一步前后都会复核，竞争或失锁均失败关闭。

所有受锁保护的外部步骤在 spawn 前调用相同围栏，确认 holder PID、heartbeat PID 与固定 flock 仍有效；同一时刻只允许一个 supervised child。spawn 后立即把 child PID 和上下文写入全局 active 登记，执行期间持续监督 holder/heartbeat；丢锁会终止并 wait 该 Windows/WSL 进程树后返回失败。`TERM`、`HUP`、`INT` 和普通 `EXIT` 共用清理路径：先终止并 wait active child，确认不再 mutation 后，再停止并 wait heartbeat、删除本次 lease、关闭 holder 通道并 wait/终止 flock holder。不得提前释放锁让旧子进程跨越 ownership window 继续运行。

实际序列是：取得全流程锁 → 预检/提交/同步 → 生成 runtime readiness generation → 构建镜像与校正 WSL 前置条件 → 以 15 秒预算停止所有已知 profile 的七个应用容器并建立 quiescence 基线 → 受控确保仅基础设施在线 → 第一次只读 NATS cutover preflight → PASS 后以 5 秒预算 full-down 并重建 quiescence 基线 → 清理悬空镜像 → 启动并等待基础设施 → 执行主交易库 + RDP 显式 schema migration job 并校验 → app up 前第二次只读 preflight → 启动应用 → 在默认 **210 秒应用健康预算**内等待 Gateway 与全部 required container → 写入不覆盖的模拟部署证据包 → 报告 `simulation_stack_healthy`。任一步骤失败都会非零终止。FS-016 protocol v1 -> v2 首次发布与回滚必须使用该 full-down/full-up 路径，禁止 rolling 或 mixed-version；无法取得有效停产/quiescence/preflight 证据时，必须在 app up 前终止。

证据包位于 WSL checkout 的 `deploy/wsl2-dev/runtime/deployment-evidence/`，包含 commit、不可变 image ID、profile、overlay、schema job 状态、非秘密 runtime readiness generation、deployment lock id、必需容器状态和 Gateway 实际 published binding，不含凭据。它同时读取并验证 `pre_full_down` 与 `post_infra_pre_app_up` 两份 schema v3 cutover preflight；两者都必须绑定同一 lock id/generation/deployed commit，且满足 `status=PASSED_WITH_TRUST_BOUNDARY`、`operation=READ_ONLY`、查询完整和 quiescence 有效。writer 校验 preflight 链、记录两份相对路径与 SHA-256，并从原始 pre/post rows 重新计算 continuity，不信任 artifact 自报摘要。最终应用健康窗内还会连续读取两次 no-secret canonical durable projection，分别保存其 SHA-256，覆盖 identity、created、四维 cursor、safety-projection config、窗口、outstanding 和状态，但不保存实际 push inbox。preflight 内的七容器 before/after 指纹必须一致，精确查询时间窗内不得有相关 Docker lifecycle event。Gateway 任一实际 HostIp 不是 `127.0.0.1`/`::1` 时证据步骤失败。它固定声明 `production_ready=false`、`trading_ready=false`，不能用作上线批准。

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

标准四进程模拟部署会将同一 generation 注入应用容器。gateway/market/decision/execution 的 strict NATS/hybrid 路径使用 Redis-only protocol v2：全局 key 为 `aats:runtime:owner:<role>`，generation 只在 payload 和 peer barrier。每个 role 在任何 NATS I/O 前 claim `PROVISIONING`，立即开始续租并启动独立 subprocess watchdog，然后执行 55 秒 takeover quarantine；build 完成后 CAS 为 `READY`。父进程与 watchdog 都用计入宿主休眠的 POSIX `CLOCK_BOOTTIME` / Windows `GetTickCount64`，并通过 POSIX pidfd / Windows process creation FILETIME 绑定父进程身份。peer 只接受同 generation、精确 role/instance、protocol v2 且 phase=`READY` 的 owner。Compose 对 generation 使用 required interpolation，Redis 使用 `noeviction`，所以缺 generation 或容量写失败均显式失败关闭。

`NatsDeliveryGate` 在未 ready/ABORT 时不交付 callback；build 期 publish 最多缓冲 4,096 条且 64 MiB，所有 peer `READY` 后先 flush，再开放 callback/background。仅 strict 四主进程 NATS/hybrid 路径注入 gate 并将 push consumer 的 `max_ack_pending` 固定为 `1`；non-strict、in-memory 和 monolith 不注入 gate，也不强制该窗口。checker 与 runtime 共用 mutable migration policy：只允许 snapshot/transient 从正数旧 `ack_wait` 向声明目标提高；event `ack_wait`、任意 `max_deliver` 或不安全非事件 `ack_wait` drift 都失败关闭。critical ALL durable 的 safety-projection immutable drift 失败关闭而不删除 cursor。首次把 critical ALL durable 从旧/无限窗口降到 `1` 前，必须先停止其生产者并取得只读证据，证明 `num_ack_pending == 0`；persist-only `system.audit_records` 没有 JetStream consumer。标准 stop 只证明应用容器不再 running，**本身不等于已证明 broker drain**。若旧窗口仍有任意未 ACK，或配置虽已为 `1` 但历史 outstanding 大于 `1`，运行时绑定固定失败关闭，不 update/bind、绝不 delete/recreate/reset cursor。

LAST/NEW snapshot/transient durable 不承载必须保留的金融事件 cursor。其 ACK-window backlog 重建只在 strict delivery-gated、policy 非 ALL，并且（outstanding 超过目标，或窗口正在收缩且 outstanding 非零）时发生；safety-projection immutable drift 另有声明丢弃语义重建分支。两者都会在新 bind 前 delete/recreate：LAST 重新取得当时最新快照，NEW 只接收重建后的事件，旧 pending/cursor 按声明语义丢弃。标准 preflight 会先阻断 immutable drift，故该运行时分支不是发布绕行许可。自动重启也可能进入分支，运行时代码不接收“已 full-down”信号。标准 full-down 只是首次 v1 -> v2 切换的额外发布门禁。ALL durable 永不走这些分支。部署 stop 范围使用所有受支持 profile 应用容器的并集，目标 profile 的 `APP_CONTAINERS` 只用于启动后的健康与证据要求，避免 derivatives -> spot 遗留 collector。

标准部署入口先 stop 所有 app，并只接受 `exited/dead` 或明确 not-found；paused/restarting/removing/unknown 和 inspect 失败全部阻断。每次 quiescence 指纹都覆盖七个固定 allowlist 容器的 ID、状态、`StartedAt`、`FinishedAt`、`RestartCount`。随后以基础设施-only Compose 受控确保 Redis/NATS/Postgres 在线（不启动 app），用 `~/aats-venv/bin/python scripts/check_nats_durable_cutover.py` 固定连接 `nats://127.0.0.1:4222`，分页枚举全部 stream/consumer。schema v3 使用 `consumer_ownership.py` 的人工 authoritative declaration；动态 assembly 测试证明它与四个 `build_runtime()` 精确一致：gateway/market/decision/execution 为 `31/8/27/11`，event/snapshot/transient 为 `49/24/4`，共 `77`。检查覆盖分页/总数、逐 stream consumer count、exact identity/role/topic/semantics、created、四维 cursor、safety-projection config、窗口和 outstanding；实际 inbox 仅记录存在性。preserved install 必须 exact `77` 且无 unowned，fresh install preflight 必须为空并在 app-up 后 final 证明 exact `77`。preflight 前后复核完整指纹，并查询精确时间窗内相关 Docker lifecycle event；它不 ACK、delete、update、recreate、reset 或 purge。第一次 PASS 后才继续 full-down 并建立新基线；正常 infra/schema 完成后、app up 前必须再取得一次 PASS。查询失败、BLOCKED 或 quiescence 失效会保持基础设施/NATS 在线并退出。最终 evidence 同时验证两份 v3 artifact 的 chain 并从原始行重算 continuity。BLOCKED outstanding 只能在人工批准变更窗保留旧状态、以匹配旧版本消费者自然 drain 到零后重跑；missing/unowned/config drift 进入人工 release review。该路径已有聚焦测试，Windows Git Bash -> WSL 锁竞争、释放与重取有窄 smoke 证据；v3 已提交且标准第一次 preflight 已按设计阻断，完整 PASS chain 与 Docker 故障矩阵仍 `OPEN`。

运行事实边界：2026-08-29 05:54Z 的最近一次标准 derivatives 尝试运行已提交 `45612697` 的 schema v3 checker。第一次 `pre_full_down` 只读 preflight 扫描 `3` 个 stream/`78` 个 consumer，识别全部 `77` 个声明 durable、无 missing，仅因 `aats-codex_manual_resume-system_operator_command_responses` 未归属而 `BLOCKED`。证据路径为 `artifacts/deployments/nats_durable_cutover_preflight_20260829T055421396560Z_4c98224d8635.json`；流程没有进入 full-down、schema、app-up 或最终 evidence。七个 app 当前保持停止，NATS/Redis/Postgres 健康在线；未知 durable 不得自动删除。

NATS 连续断连 30 秒或永久关闭会变为 runtime critical failure。现存 durable 在 update/bind 前冻结 broker `created`、真实 push inbox 与四维 cursor；update 回读、post-bind、READY 前和稳态监督都拒绝同名重建、inbox 漂移、cursor 回退及 safety-projection config drift。实际 inbox 不进入日志/证据。flow control/idle heartbeat 历史差异当前保留并告警，不在 qualification/supervisor 投影内。consumer management 查询、push binding 或 idle-heartbeat 持续异常达到 30 秒进入 terminal；gate 已激活且存在 backlog 时，游标/待处理量在 `max(30 秒, 2 x ack_wait)` 内无进展也进入 terminal。post-bind 失败只 abort gate 并有界取消本地 subscription，不 drain/ACK/delete broker durable。Redis claim/replace/poll/refresh 失败、peer 60 秒未就绪或缺 generation 均在对外可工作前失败。真实 NATS 集成已证明 core TCP 仍连接时删除 critical durable 会 ABORT gate 并触发 terminal；这不等于真 Docker 服务、网络分区、backlog stall 与告警矩阵已经通过，后者仍为 `OPEN`。

owner TTL 为 60 秒，每 10 秒续租，安全停机 margin 为 30 秒，hard-exit grace 为 10 秒。每次成功 `PROVISIONING` 写入或续租后，本地 hard fence 最多滑动到该次写入后 50 秒；另有 claim 到 `READY` promotion 的 180 秒绝对上界，续租不得延长，并在第 170 秒冻结续租、进入最后 10 秒 fatal grace。确定 ownership 丢失零宽限。关键故障先冻结续租并进入不可 disarm 的 fatal deadline；正常停机先冻结续租并以 10 秒硬截止执行 cleanup，业务与 NATS 安全停止后才 disarm watchdog、owner-aware delete。其他路径保留 key 等待 TTL fencing。该候选不是下游执行端强制校验的单调 fencing token；Redis owner truth 与 watchdog/OS 终止同时失效的双故障尚未取得排他证明。最终真 Redis/NATS/Docker 故障注入、标准发布和逐角色重启矩阵均 `OPEN`，不得视为 startup/restart PASS 或 live 放行依据。

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
| peer readiness ownership | deployment evidence + 四主进程日志 + Redis TTL/NATS consumer 只读观察 | 四个全局 `aats:runtime:owner:<role>` key；owner 都是 protocol v2、唯一 instance、同一非空 generation 且 phase=`READY`；无 v1/mixed-version 进程；运行跨越 60 秒 TTL 和多个 10 秒 renewal 周期；验证 claim 后即续租、55 秒 quarantine、每次成功 PROVISIONING 写/续租后的 50 秒滑动 hard fence、claim→READY 180 秒绝对上界（170 秒冻结 + 10 秒 fatal grace）、30 秒 READY safety margin、正常 cleanup 10 秒硬截止、确定失租零宽限、连续断连 30 秒 critical failure。仅 strict NATS/hybrid 验证 gated `max_ack_pending=1` 和 critical durable cursor 未被删除。首次 cutover 另需全流程 WSL lock 下生成两次 schema v3 全量分页只读 preflight PASS；preserved install 必须精确覆盖声明的 `77` 个 consumer 且 missing/unowned 均为零，proven fresh install 的 preflight 必须为零并仅在 app-up 后 final 要求 exact `77`。第二次证据必须与最终包的 lock id/generation/deployed commit 一致，并包含七容器稳定指纹、零 lifecycle events、相对路径与 SHA-256；writer 还须独立重算 preflight continuity，并封存最终应用健康窗内先后两次 no-secret canonical durable projection 及各自摘要。标准 stop 不可替代 drain/quiescence 证据。首发/回滚必须 full-down；目标发布、双故障、下游 fencing、重启/断连及完整 Docker consumer supervision 矩阵未跑前保持 OPEN |
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
