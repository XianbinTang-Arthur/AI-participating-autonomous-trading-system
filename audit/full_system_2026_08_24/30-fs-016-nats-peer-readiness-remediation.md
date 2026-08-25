# 30 FS-016 NATS Peer Readiness 失败关闭与部署代次隔离整改

> 阶段：Phase 3J  
> 日期：2026-08-24  
> Git 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：`codex/fs-002-kill-switch-p0` 上 Phase 3A–3J 未提交叠加变更  
> 当前裁定：`CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 执行摘要

FS-016 在当前未提交工作区完成了代码级整改。gateway、market、decision、
execution 四个主进程使用 `hybrid` 或 `nats` event bus 时，跨进程 peer
readiness barrier 不再在 Redis announce/poll 异常或 60 秒超时后 warning 并继续。
上述异常、缺少 hot-state store 或缺少部署 generation 均在任何 background
publisher 启动前失败。

每次标准模拟部署会在 Windows 工作区同步到 WSL2 后、构建镜像前生成一个
非秘密 runtime readiness generation。Compose 使用 required interpolation 注入；ready key
与 payload 都绑定该代次，旧部署残留 key 不能满足新部署 barrier。该代次
还会记入 Phase 3F 建立的不可覆盖模拟部署证据包，但证据包仍固定声明
`production_ready=false` 和 `trading_ready=false`。

本阶段只执行 Windows 本地静态检查、隔离单元测试和文档核对。没有启动
WSL2/Docker，没有连接 Redis/NATS/PostgreSQL/交易所，没有读取 `.env.*`。因此
FS-016 不能标为 CLOSED，更不能将单元测试作为真实四进程启动/重启通过证据。

## 2. 原始问题与投递风险

当前 `AATS_EVENTS` JetStream 使用 `INTEREST` retention。当对应 durable consumer 尚未
建立时，不能依赖该 stream 像 LIMITS 一样为无兴趣方保留消息。交易命令类 topic
已拆到 `AATS_EVENTS_COMMANDS` 并保持 LIMITS，但一般事件流仍包含 order update、
fill、reconciliation、kill-switch 等状态传播。

修复前路径为：

```text
build_runtime + register durable subscriptions
  -> Redis SET aats:runtime:ready:<role>
       failure -> warning + continue
  -> Redis GET peer fixed keys
       failure/60s timeout -> warning + continue
  -> start_background_tasks
       publisher may emit before this deployment's peers are provisioned
```

固定 role key 只有 300 秒 TTL，没有部署代次。标准 deploy 会保留 Redis 基础设施，
新容器可能在旧 key 过期前将它误认为“本次部署 peer 已注册 consumer”。这两条
路径组合后，会让启动窗口内状态事件丢失或视图漂移，与 INTEREST 契约不闭合。

## 3. 实施后的启动控制流

```text
deploy sync to WSL2 checkout
  -> generate <head-short>-<UTC>-<pid>-<random>
  -> inject AATS_RUNTIME_READINESS_GENERATION into Compose
  -> build/down/infra/schema/app up

each split main process
  -> resolve strict gate: role in main roles AND backend in hybrid/nats
  -> require valid generation before runtime/schema side effects
  -> build_runtime and provision role subscriptions
  -> SET aats:runtime:ready:<generation>:<role>
       payload = generation + process_role + ready_ts + pid
       Redis/no-store failure -> fixed RuntimeError
  -> GET exact same-generation peer keys
       invalid payload / Redis failure / timeout -> fixed RuntimeError
  -> only after all peers match: start_background_tasks
  -> graceful exit: best-effort delete own generation-scoped key
```

Gateway 通过 FastAPI lifespan 执行同一契约；market/decision/execution 通过
`run_process()` 执行。Gateway 缺 generation 时在 RDP schema validate 和 runtime build 前失败；
worker 缺 generation 时在 `build_runtime()` 前失败。barrier 失败后不设置可对外使用的
runtime，不启动 dashboard plane、business background tasks 或 heartbeat ready。

## 4. 代码和配置变更

| 文件 | 变更 | 契约意义 |
|---|---|---|
| `aats/bootstrap/settings.py` | 新增/normalize generation，限长 128，只允许安全 key 字符 | 阻止空值、路径/空白注入和无界 key |
| `aats/bootstrap/process_lifecycle.py` | strict 判定、代次 key/payload、announce/poll/timeout 失败关闭、退出撤回 | publisher 前不得跳过 peer provisioning |
| `apps/api_gateway/main.py` | Gateway 在 schema/build 前校验 generation，在 publisher 前执行 strict barrier，失败清理 runtime | HTTP lifespan 不能绕过 worker 入口的同一安全契约 |
| `deploy/wsl2-dev/docker-compose.aats.yml` | common env 使用 required generation interpolation | 手工 Compose 缺代次不会默默回落 |
| `scripts/deploy.sh` | sync 后/build 前生成代次，每次 app Compose 调用注入 | 同一次标准部署的四主进程共享代次 |
| `scripts/write_deployment_evidence.py` | 校验并记录非秘密 generation | 容器、image、commit 与 barrier 代次可事后对齐 |
| `tests/unit/test_fs016_nats_peer_readiness_fail_closed.py` | 9 项 FS-016 定向契约 | 固化代次、strict failure、Gateway/worker 无副作用与 deploy 传递 |
| `tests/unit/test_process_lifecycle_readiness_gate.py` | 更正旧 LIMITS fallback 注释 | optional/in-memory 兼容不再被误写成 NATS 现行契约 |

## 5. 代次、TTL 与重启边界

generation 只允许字母数字、点、下划线、冒号和连字符，不含凭据、账户、主机
身份或连接串。ready key 继续使用 300 秒 TTL，但优雅退出会 best-effort 删除本
role/本 generation key；删除失败只记异常类型，不覆盖原始退出原因。

该 key 的语义是“本部署代次已完成 durable consumer provisioning”，不是进程持续存活
或业务已持续前进的 lease。同 generation 的单容器重启可复用其他 peer 的 provisioning
事实；JetStream durable consumer 在 subscriber 短暂离线时仍形成 interest。进程是否活着、
critical task 是否前进、依赖是否新鲜仍属 FS-006/G4。

崩溃残留、NATS 同时重启、consumer store 丢失与同代次多容器并发重启尚未做
真实故障注入，不能由上述设计直接推定为 PASS。

## 6. 错误、日志与敏感信息边界

strict 失败对上层只抛固定 `RuntimeError` 代码：

- `runtime_ready_gate_generation_required:<role>`；
- `runtime_ready_gate_hot_state_required:<role>`；
- `runtime_ready_gate_announce_failed:<role>`；
- `runtime_ready_gate_poll_failed:<role>`；
- `runtime_ready_gate_timeout:<role>:<missing peers>`。

Redis 底层异常正文不进入 barrier 结构化日志或上层错误，避免 URL/密码等
连接细节被转发。日志可记录 role、非秘密 generation、missing peers、timeout
和 exception type。本阶段没有读取或展示 `.env.*`。

## 7. 验证证据

### 7.1 定向与扩大回归

首次 FS-016/process/Gateway/deploy 聚焦回归为 `113 passed`。复核后又将 strict 底层
函数的 generation 不变量下沉、增加 Gateway 在 schema/build 前失败的对抗测试，并
将 generation 纳入部署 evidence。最终扩大回归为 `129 passed, 1 warning`。

### 7.2 最终结果

| 验证 | 结果 | 证明范围 |
|---|---:|---|
| FS-016 新测试 | 9 项纳入全量并通过 | generation、strict announce/wait、Gateway/worker 副作用边界、deploy 传递 |
| FS-016/process/Gateway/deploy/FS-005/006/007/009 扩大回归 | `129 passed, 1 warning` | 相邻生命周期、证据包、loopback 和部署失败关闭未回归 |
| 全量 unit | `4286 passed, 30 skipped, 1666 warnings, 85 subtests passed` | 109.12 秒，无断言失败 |
| Ruff `aats/ --fix` | PASS | 零剩余错误 |
| Ruff FS-016/apps/scripts/tests | PASS | 零错误 |
| `bash -n scripts/deploy.sh` | PASS | shell 语法通过，不代表部署运行通过 |
| Compose YAML safe parse | PASS | YAML 结构可解析，未执行 Compose interpolation/runtime |
| 变更 Markdown 本地链接 | 69 files / 306 links / 0 broken | 只检查当前修改和未跟踪 Markdown |
| `git diff --check` | PASS | 仅有仓库现有 LF/CRLF 转换提示，无 whitespace error |

pytest 的单条聚焦 warning 来自 Windows `.pytest_cache` 目录创建冲突。全量中的
1,666 条既有 warning 主要是 SQLite datetime adapter deprecation 和
`test_long_short_poller.py` AsyncMock coroutine 未 await；它们属 FS-021 治理欠账，
本阶段没有隐藏或声称已修复。

## 8. 未执行与未知项

本阶段明确未执行：

1. WSL2、Docker Compose、容器构建/启动/重启或任何 deploy；
2. 真实 Redis set/get/TTL/断连或多进程 key 竞态；
3. 真实 NATS JetStream consumer provisioning、INTEREST 消息计数或 stream restart；
4. peer 慢启动、部分进程 crash、同代次重启和旧代次残留故障矩阵；
5. 真实/clone PostgreSQL、交易所、账户、余额、订单或仓位；
6. 告警送达、容器 restart policy、恢复时间和独立 reviewer 复核。

上述项目全部保持 `UNKNOWN`/`OPEN`。单元测试不能外推为实际 Redis/NATS 已按
预期配置，静态 Compose 字符串也不能外推为已运行容器环境一致。

## 9. 关闭条件

FS-016 只有在下列条件全部满足后才可重新评估 CLOSED：

1. 在隔离生产等价四进程环境运行全新部署、单容器重启和整栈重启；
2. 对 Redis announce 前/后、poll 中和恢复后断连，证明任一失败均无 publisher/无 ready；
3. 注入 NATS consumer provisioning 延迟/失败，证明超时非零且没有关键事件缺口；
4. 预置旧 generation、畸形 payload、同 role 旧 PID 和 TTL 边界，旧事实不能满足新 deploy；
5. 核对 deployment evidence、四主进程日志、Redis key 与 NATS consumer identity 的代次一致性；
6. 将 task liveness/last-success/lag 与本启动 provisioning 事实分开验收，不用 ready key 代替 FS-006；
7. 独立 reviewer 复核实现、故障矩阵、消息计数和无敏感信息证据。

任一项 UNKNOWN 都不得改写为 PASS 或 CLOSED。

## 10. 最终裁定

```text
FS-016: CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN

REAL-MONEY PRODUCTION: NO-GO
```

本阶段消除了当前代码中“INTEREST stream + readiness 失败后继续发布”以及
“旧部署 ready key 满足新部署”的已知路径。它没有证明目标 NATS/Redis/Compose
在启动、重启和断连下的实际行为，不构成部署、真实资金操作或上线授权。
