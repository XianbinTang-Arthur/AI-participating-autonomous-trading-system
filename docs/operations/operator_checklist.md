# 运维检查清单 (Operator Checklist)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 文档状态：现行操作检查清单
> 最后核对：2026-08-29（核对基线 `main@f9bb24996436` + 当前 FS-016 NATS exact-ownership 候选；以本文档所在最终 HEAD 为准）。当前标准入口禁用 live；下列 live 项只作为重新开放前的 future 检查，不代表已有运行状态。


## 日常巡检

### 每日 / 每次运行后

- [ ] 运行质量巡检
  ```bash
  python scripts/rdp_run_quality_monitor.py
  ```
  - 检查 health 状态: healthy / degraded / unhealthy
  - 如有 critical failure，立即排查

- [ ] 检查最近 round 状态
  ```bash
  python scripts/rdp_list_active_rounds.py
  ```
  - 确认最近 round 是否 succeeded
  - 如有 failed/partial，查看失败原因

- [ ] 检查参数与发布状态
  - 在 Operator 中查看 `GET /rdp/parameters/active`、`GET /rdp/recommendations/latest` 和 `GET /rdp/releases/latest`
  - 确认当前 active 版本、推荐状态、gate/release/apply history 一致

- [ ] 检查治理层 DB 连通性和 active parameters
  ```bash
  python scripts/rdp_run_reliability_check.py
  ```
  - 登录 Operator 后核对 `GET /rdp/health` 和 `GET /rdp/parameters/active`
  - 确认 DB 连接正常、active combo/version/actor 可追踪
  - runtime active parameters 以 Postgres 为唯一真源，不做 DB/JSON 一致性 seed

---

## 新 Round 运行前

- [ ] 确认数据窗口
  - 数据是否已 backfill 到目标时间范围
  - Gold 表是否有数据

- [ ] 确认参数版本
  - 是否需要使用 `--params-json` 注入 Phase 2 推荐参数
  - 默认参数 vs 推荐参数，是否有意为之

- [ ] 确认数据库连接
  - Phase 3 需要 live DB (或 `--replay-only`)
  - Phase 4 需要 Gold OHLCV 数据

---

## 新 Round 运行后

- [ ] 检查退出码
  - 0 = 全部成功
  - 2 = 部分成功 -> 查看哪些 combo 失败
  - 3 = 全部失败 -> 排查数据/连接问题

- [ ] 检查产物完整性
  ```bash
  python scripts/rdp_validate_artifacts.py
  ```

- [ ] 更新 artifact index
  ```bash
  python scripts/rdp_build_artifact_index.py
  ```

- [ ] 更新 active round index
  ```bash
  python scripts/rdp_list_active_rounds.py
  ```

---

## 参数管理

`rdp_freeze_parameter_set.py` 已被批次 A 硬化禁用，当前没有受支持的候选 registry 手工 freeze/import CLI。不要直接改数据库替代产品化入口。

- [ ] recommendation 审批/拒绝/替代只走 Operator API。
- [ ] apply/release/rollback 只走带认证、gate 和 `X-Rdp-Apply-Token` 的 API。
- [ ] 若确需尚未 API 化的 registry 维护，停止操作并由维护者先设计、实现和审查受控入口。
- [ ] 完整流程见 [参数应用与回滚](parameter_apply_and_rollback.md)。

---

## 故障排查

### opening_count = 0

1. 检查参数: `min_safe_net_edge_bps` 是否太高
2. 检查数据: 该时间窗口的 bar 数据是否存在
3. 尝试放宽参数重跑

### 全部 combo 失败 (exit code 3)

1. 检查数据库连接
2. 检查数据是否已 backfill
3. 检查 stderr 日志
4. 使用 `rdp_retry_failed_round.py --action plan` 生成诊断

### manifest 校验失败

1. 只读运行 `rdp_validate_artifacts.py --phase <phase>` 定位字段和路径错误
2. 停止使用该 round，不要原地修改 `round_manifest.json`
3. legacy 证据必须迁移为新的 artifact/round，重新建立 digest 与 index，并经过审查

### 质量巡检 unhealthy

1. 查看 `quality_monitor_summary.json` 中 `passed: false` 的检查项
2. 按 category 分类处理:
   - `artifact`: 文件/目录缺失
   - `result`: 结果异常
   - `parameter`: 参数文件问题
   - `governance`: 治理文件缺失

---

## 交接须知

新接手人员应:

1. 阅读 [平台运行手册](platform_runbook.md)
2. 阅读 [Artifact 规范](artifact_conventions.md)
3. 运行质量巡检确认平台状态
4. 查看参数注册表了解当前有效参数
5. 查看 active round index 了解最近运行情况

---

## 主交易系统 live 前检查

> 本节不是 RDP 日常巡检，而是任何 `spot_live` / `derivatives_live` 真实提交前的人工检查。

### 必须确认的启动条件

- [ ] 当前 profile 与账户一致：`spot_live` 是 spot/cash，`derivatives_live` 是 derivatives/cross/hedge。
- [ ] committed candidate 的 managed strategy YAML 已通过 mapping/unknown-key 校验；没有仓库外 overlay 继续写已删除的伪 auto-rollback key。
- [ ] `AATS_STORAGE_MODE=postgres`。
- [ ] `AATS_DATABASE_URL` 指向对应 live 数据库，不与模拟盘/研究库混用。
- [ ] `AATS_DATABASE_SINGLE_RUNTIME_GUARD_ENABLED=true`。
- [ ] `AATS_EXECUTION_BACKEND=okx`。
- [ ] `AATS_ACCOUNT_BACKEND=okx`。
- [ ] `AATS_ACCOUNT_READ_ENABLED=true`。
- [ ] `AATS_OPERATOR_AUTH_ENABLED=true`。
- [ ] `AATS_OPERATOR_UNSAFE_WRITE_WITHOUT_AUTH=false`。
- [ ] OKX 凭证、Operator session secret 只存在于 gitignored `.env.*`，没有出现在日志、commit 或文档中。

### 必须确认的运行状态

- [ ] Gateway 实际 Docker published HostIp 仅为 loopback，并与最新模拟 evidence 一致；静态 Compose 不能替代 runtime inspect。
- [ ] 目标主机防火墙、VPN/NAT、TLS/证书与非授权网络不可达性有本次只读证据；未验证时标 UNKNOWN。
- [ ] 从目标 HTTPS 入口检查 CSP、`DENY`、`nosniff`、`no-referrer`、Permissions Policy、COOP/CORP 和 HSTS 各只有有效策略，proxy 未删除、重复或降级。
- [ ] 真实浏览器登录、UI 导航和 API 请求无 CSP violation；不受信 Host 返回 400 且不回显输入。
- [ ] HTTP 模拟入口不带 HSTS；只在证书、域名和 HTTPS 重定向闭环成立后验证 HSTS，不要在错误域名上人工缓存。
- [ ] 在实际 Gateway 进程数和受信 proxy 拓扑下验证登录集中限流不可由多进程、重启或伪造 forwarding header 绕过；当前代码只有每进程 60/20/10 窗口。
- [ ] 在隔离生产等价 DB/KDF 硬件验证正确/错误/不存在用户混合负载的 p95/p99、event-loop lag、DB pool wait、429/503 拒绝率和紧急登录 SLA；默认 concurrency 4/queue 1s 不是容量验收。
- [ ] FS-008 目标容量证据覆盖全部 daemon、两个 collector、RDP、慢查询、DB 短断/重连、进程重启和恢复/admin 竞争；记录每服务 checked-out/overflow/wait/timeout、PostgreSQL 峰值/拒绝、联合内存和告警送达。声明 topology 150、普通容量 197、名义余量 47 只是静态预算，不可勾选替代实测。
- [ ] protocol v1 -> v2 首次发布或回滚走标准 full-down/full-up；不做 rolling upgrade、mixed-version 运行、单角色跨协议替换或手工 owner key 操作。标准入口必须在任何 mutation 前取得固定 `/tmp/aats-standard-deploy.lock` 的长寿命 WSL `flock`；生产不得设置 `AATS_DEPLOY_LOCK_FILE`，只有 `AATS_DEPLOY_TEST_MODE=true` 的隔离测试可覆盖。确认 Windows 3 秒 heartbeat / WSL 12 秒失联释放、fresh predecessor lease 接管隔离、竞争与失锁均失败关闭。
- [ ] 首次 ACK-window cutover 必须使用标准入口：stop 所有已知 profile 的七个 app 后只接受 `exited/dead` 或明确 not-found；paused/restarting/removing/unknown/inspect 失败立即阻断。quiescence 证据必须记录并前后比较容器 ID、状态、`StartedAt`、`FinishedAt`、`RestartCount`，且精确查询区间内不得有相关 Docker lifecycle event。入口随后仅启动基础设施/NATS（不得 app up），用规定 `~/aats-venv` 从 loopback 全量分页读取全部 stream/consumer，并与 `consumer_ownership.py` 的人工 authoritative declaration 比较；动态 assembly 测试必须证明它与四个 `build_runtime()` 精确一致。当前 manifest 为 `77` 个 durable：gateway `31`、market `8`、decision `27`、execution `11`，覆盖 event `49`、snapshot `24`、transient `4`；persist-only `system.audit_records` 不在 JetStream 集合。必须核对完整分页/计数、每个 stream 的 consumer 数量、stream/durable/role/topic/semantics、created、四维 cursor、safety-projection immutable/mutable config、窗口与 outstanding；实际 inbox 不得写入证据，只记录存在性。`existing_container_preserved` 下声明 consumer 缺失或实际 consumer 未归属都阻断；`proven_fresh_install` 的 preflight 要求 consumer 集合为空，app-up 后最终证据才要求 exact `77`。标准 stop 本身不是 drain 证据；基础设施-only up 后、full-down 前与重建 quiescence 基线后的 app-up 前都必须取得 PASS。最终 deployment evidence 必须同时验证这两份 schema v3 artifact 的同 lock id/generation/deployed commit、`PASSED_WITH_TRUST_BOUNDARY`、`READ_ONLY`、完整查询、quiescence、chain、相对路径与 SHA-256，并保存最终健康窗内两次 no-secret canonical durable projection 及各自 SHA-256。
- [ ] event durable 的旧/无限窗口仍有任意未 ACK，或旧 config 已为 `1` 但 outstanding 大于 `1`，都必须失败关闭；运行时绑定使用固定码 `nats_critical_consumer_ack_window_migration_requires_drain:<durable>`。共享 mutable migration policy 只允许 snapshot/transient 从正数旧 `ack_wait` 向声明目标增加并原位回读；event 的 `ack_wait` drift、任意 `max_deliver` drift、其他 immutable safety-projection drift，以及 preserved install 的 missing/unowned consumer 都必须阻断并进入人工 release review。preflight BLOCKED/查询失败必须保持 NATS 与持久状态在线。event outstanding-only 恢复只能在人工批准的变更窗由匹配旧消费者自然 drain 到零后重跑；不得自动 ACK、删除 event durable、reset cursor、purge 或人工确认业务消息绕过。LAST/NEW 的 ACK-window backlog 重建与 immutable-drift 重建是两个分支，均只能按已声明 snapshot/transient 丢弃语义执行；标准 preflight 会先阻断 immutable drift。
- [ ] 部署证据包中的 runtime readiness generation 与 gateway/market/decision/execution 结构化日志完全一致；Redis 只有四个全局 `aats:runtime:owner:<role>` key，owner 的 protocol 为 v2、instance 唯一、generation 精确一致且 phase=`READY`。generation 不得进入 key，不得有 v1/mixed-version owner。
- [ ] 四个 owner 从 `PROVISIONING` claim 起就续租，先经历 55 秒 takeover quarantine，再跨越 TTL 60 秒和多个 10 秒 renewal 周期；证明父/child 使用 POSIX `CLOCK_BOOTTIME` 或 Windows `GetTickCount64`，且 pidfd/creation FILETIME 身份围栏拒绝 PID 重用。不得用单一容器 healthy 或一次瞬时 TTL 读取代替证明。
- [ ] 故障注入分别证明：每次成功 PROVISIONING 写/续租后至本地 hard fence 最多 50 秒；claim→READY promotion 绝对不超过 180 秒，且第 170 秒冻结续租并进入最后 10 秒 fatal grace；续租不能延长绝对上界；READY 30 秒 safety margin、确定失租零宽限。关键故障立即冻结续租并保持不可 disarm 的 fatal watchdog；正常 shutdown 必须先冻结续租、在 10 秒硬截止内停止业务/NATS，随后才 disarm 和 owner-aware delete。其他路径保留 TTL fencing。
- [ ] 在隔离生产等价 Redis/NATS/Docker 运行新部署、受控单角色重启、peer 延迟/失败、Redis claim/replace/poll/refresh 断连、NATS 连续断连 30 秒与旧 generation payload 残留矩阵；Redis 必须为 `noeviction`。失败时 delivery gate 必须 `ABORT`，无 callback parse/persist/handler/ack/nak，无网络 publish，无 background/伪 ready。
- [ ] 在 strict 四主进程 NATS/hybrid 制造 build 期 publish 并验证最多 4,096 条且 64 MiB 双上限、gated push consumer `max_ack_pending=1`；另验证 non-strict/in-memory/monolith 不注入 delivery gate、不强制该窗口。已有 durable 更新后必须从 broker 回读一致；event durable 不删除 cursor，snapshot/transient 也只能执行 manifest 允许的 `ack_wait` 增加或声明丢弃语义重建。所有 peer `READY` 后先 flush，再开放 callback/background，flush/ABORT 竞态不丢失败关闭。
- [ ] 验证 critical consumer runtime supervision：durable `NotFound`，broker `created`/durable name/实际 push inbox 变化，四维 cursor 任一回退，或 ack/deliver/filter/replay/headers/pause/backoff/rate-limit/inactive-threshold/mem-storage/start-position/ack_wait/max_ack_pending/max_deliver safety-projection 漂移立即 terminal。现存 durable 必须在 update/bind 前冻结 identity/inbox/cursor，update 回读、post-bind、READY 前和稳态核验都保持连续；post-bind 失败只能 abort gate 并有界取消本地 subscription，不 drain/ACK/delete broker durable。flow control/idle heartbeat 历史差异当前仅保留并告警，不在 qualification/supervisor 内，必须作为 OPEN 运行边界单独验收。management 查询、push unbound 或 heartbeat inactive 持续 30 秒有界 terminal；gate 激活且 backlog 存在时，进度在 `max(30 秒, 2 x ack_wait)` 内无变化也 terminal。真实 NATS consumer-delete 集成测试已通过，但真 Docker 网络、push/heartbeat、backlog stall、容器退出与告警送达矩阵仍须逐项验证。
- [ ] 标准 deploy 的实现顺序为“全流程持锁 → 停应用并建立 quiescence 基线 → 基础设施-only up → 第一次只读 NATS preflight → PASS 后 full-down/新基线 → 正常 infra/schema → app-up 前第二次 preflight → app/health/evidence/report”，应用健康检查默认使用 210 秒预算。每个外部步骤必须 spawn 前复核 holder/heartbeat/flock，spawn 后全局登记唯一 active child；活动中失锁，或部署 shell 收到 `TERM/HUP/INT/EXIT` 时，必须先终止并 wait Windows/WSL 子进程树，再停止 heartbeat、移除 lease、释放/wait flock holder。2026-08-29 03:43Z 的标准 derivatives 尝试使用 `f9bb2499` 旧 schema v2 checker 走到第一次 preflight：扫描 `78` 个 consumer，只识别 `49` 个 event，故把 `28` 个合法非事件 consumer 和 `1` 个 `aats-codex_manual_resume-system_operator_command_responses` 一并列为 `29` 个 unexpected 后安全阻断。七个 app 保持停止，NATS/Redis/Postgres 在线，没有进入 full-down/app-up。该旧 artifact 不得冒充 v3 资格证据；候选 v3 对同一 broker 的非发布只读诊断得到 `77+1`，仍须提交后用标准入口重跑确认。未知 durable 只能由真人 owner/release review 处置，不得自动删除，也不得用手工 Compose 绕过。
- [ ] 停应用必须覆盖所有受支持 profile 应用容器并集，而非只覆盖目标 profile；显式验证 derivatives -> spot 不遗留两个 collector。preflight 对缺失/None/畸形的 outstanding 或 cursor 必须 QUERY_FAILED，不得按零处理。LAST/NEW 的 ACK-window backlog 重建只在 strict delivery-gated、policy 非 ALL，并且（outstanding 超过目标，或窗口正在收缩且 outstanding 非零）时发生；其 safety-projection immutable drift 另有声明丢弃语义重建分支。自动重启也可能触发，标准 full-down 只是额外发布门禁而非运行时信号。ALL cursor 永不删除。
- [ ] 明确记录 residual risk：owner lease 不是下游执行端校验的单调 fencing token；Redis owner truth 与 watchdog/OS 终止同时失效的双故障尚无排他证明。上述故障矩阵和下游 fencing 未关闭前，FS-016 不能作为 live 放行依据。
- [ ] `/healthz` 返回 200；这只证明 FastAPI 存活且当前进程内 supervisor 未发现关键 task 结束或纳管固定周期 task stalled，不覆盖全部事件驱动任务，也不是 trading-ready 信号。
- [ ] `/system/health` 无 critical blocker。
- [ ] `aats-rdp-daemon` 健康；future derivatives-live required list 已包含 `aats-liquidations-daemon` 和 `aats-microstructure-collector`，但当前 live 禁用，必须保留为未验证而非勾选通过。
- [ ] Kill switch 状态明确；如果打开，必须有 operator 记录说明原因。
- [ ] account snapshot fresh，且账户产品类型、保证金模式、币种与 profile 一致。
- [ ] reconciliation 最近报告无 unresolved high/critical finding。
- [ ] execution command queue 无 `PENDING` submit/cancel 积压。
- [ ] 无 stale `SENT` submit；如果存在，先按 client order id 对交易所查询并进入人工恢复流程。
- [ ] active parameter set 的 version、actor、gate status、apply history 可追踪。

### 人工确认项

- [ ] live submit 开关、kill switch、runtime mode 三者状态一致。
- [ ] Gateway/monolith 的 Kill Switch permission lease task 正常，generation 与长期 RUNNING authority 一致；execution 不具备续租能力。当前尚无目标环境 PASS 证据时，此项只能记 UNKNOWN，不能据此放行 live。
- [ ] 当前 active parameter set 有清晰的审批、gate、apply history。
- [ ] 本次启动前的代码版本、profile、数据库和 OKX 账户已记录。
- [ ] 如有人工恢复、手动取消或参数回滚，已写入操作备注。

---

## 如何区分 "advisory-only candidate" vs "execution outage"（很容易误判）

`strategy.portfolio_allocation_decisions` 的 `route_action=advisory_only` 有两种完全不同的语义，
看错会导致"系统其实健康但被误判为故障"或"系统已经坏了被当作正常 shadow 输出"。

### advisory-only candidate（设计态，**不是故障**）
- reason_codes 包含 `independent_family_candidate_inactive` /
  `candidate_execution_incompatible` / `legacy_configured_strategy_family_independent_hold_only`
- sleeve_intents[].legs = [] 且 metrics.permission.permission_mode = `unsupported`
- metrics.composition.execution_behavior = `hold_current`
- 上游链路：`independent_family.py::_independent_execution_compatibility`
  `bool(result.legs or overlay_decision.active)` = False → `sleeve_execution_permission.py`
  把 approved_for_execution 降成 False → allocator 产 advisory_only
- 根因：当前 baseline 信号条件（score / net_edge / regime）下没有 executable legs
- **操作**：这是设计行为，不要 restart 也不要重 deploy；如需 independent 下单，要看
  `docs/review/allocator_budget_zero_root_cause_2026_04_19.md` 以及 P1-A/B 系列任务

### execution outage（真故障）
- 容器 `aats-execution` 不 healthy
- `/system/health` blockers 里有 `okx_ws_down` / `execution_outbox_pending_*_minutes`
- reason_codes 里出现 `execution_health_not_ok` / `kill_switch_engaged`
- 最近 `decision_target_sizing_resolved` 日志里 `policy_blocked=True` 或 `risk_capped=True`
- **操作**：立即按根目录 `DEPLOYMENT.md` 的 trading-ready、recovery、reconciliation 和安全停机步骤处理；`workflow_failure_recovery.md` 只处理 RDP workflow，不处理交易执行故障。

### 快速辨别（一条命令）
```bash
# 最近 12 小时 independent intent 的 reason 分布；命中 candidate_execution_incompatible
# 但不命中 execution_health_not_ok 即 advisory-only candidate 设计态.
docker exec aats-postgres psql -U admin aats_live_derivatives -c "
SELECT
  COUNT(*) FILTER (WHERE payload::text LIKE '%candidate_execution_incompatible%') AS advisory_candidate,
  COUNT(*) FILTER (WHERE payload::text LIKE '%execution_health_not_ok%')       AS real_outage,
  COUNT(*)                                                                      AS total
FROM event_store
WHERE topic='strategy.portfolio_allocation_decisions'
  AND event_timestamp > now() - interval '12 hours';"
```

**判断规则**：`advisory_candidate / total > 0.5` 且 `real_outage = 0` → advisory-only，正常。
