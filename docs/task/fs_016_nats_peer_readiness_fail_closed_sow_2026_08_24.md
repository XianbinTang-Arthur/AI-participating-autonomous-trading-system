# FS-016 NATS Peer Readiness 失败关闭与部署代次隔离 SOW

> 文档状态：历史实施约束；正文只保留 2026-08-24 当时设计，不是现行协议
> 日期：2026-08-24  
> Git 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：Phase 3A–3I 未提交叠加变更  
> 最后核对：2026-08-28（仅核对历史状态与现行替代入口；正文不重写）
> 目标裁定：`CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

> **已被替代，禁止作为现行操作依据**：运行现场证明本文的 300 秒一次性 key
> 与同 generation 单角色重启目标冲突。现行 protocol v2、全局 role owner key、
> `PROVISIONING -> READY` 两阶段所有权、claim 即续租、55 秒 takeover quarantine、独立
> subprocess watchdog、strict NATS gated `max_ack_pending=1`（non-strict/in-memory/monolith 不注入
> gate）、每次成功 PROVISIONING 写/续租后的 50 秒滑动 hard fence、claim→READY 180 秒绝对上界、
> 30 秒持续断连监督、Redis `noeviction`、固定生产路径的全流程长寿命 WSL `flock`（仅测试可覆盖）、
> fresh predecessor lease takeover quarantine、外部步骤 spawn fencing/active-child 登记与信号退出时
> child-first cleanup、full-down 前与 app-up 前两次
> 基础设施-only loopback 只读 cutover preflight、绑定 lock/generation/commit/quiescence 的最终证据
> path/hash、210 秒部署健康预算、首发/回滚与验收边界全部以
> [`fs_016_runtime_readiness_lease_restart_safety_p0_sow_2026_08_28.md`](fs_016_runtime_readiness_lease_restart_safety_p0_sow_2026_08_28.md)
> 为准。LAST/NEW 运行时重建由 strict gate、非 ALL policy，以及“outstanding 超过目标”或
> “收缩窗口且 outstanding 非零”条件决定，标准
> full-down 只是额外发布门禁。真实标准部署、真 Redis/NATS/Docker 完整故障注入、双故障和下游
> fencing 仍 `OPEN`，真实资金继续
> `NO-GO`。以下正文不回写，用于追溯当时问题与决策。

## 1. 背景与问题定义

FS-016 已确认一般事件流 `AATS_EVENTS` 使用 JetStream `INTEREST` retention，但四主进程
的 Redis peer readiness barrier 在 announce 写入异常、poll 异常或 60 秒超时后只记录
warning 并继续启动 publisher。`INTEREST` 在 interested durable consumer 尚未建立时不为其
保留消息；因此该 fallback 与当前 stream 契约不相容。

现有 ready key 是固定 `aats:runtime:ready:{role}`，只有 5 分钟 TTL，没有部署代次。
停止旧应用但保留 Redis 基础设施的标准部署窗口中，新进程可能读取旧部署 key 并误判 peer
已完成本次 consumer 注册。注释和旧测试仍把“LIMITS fallback”当作现行事实，已与
`AATS_EVENTS retention=INTEREST` 漂移。

## 2. 目标与非目标

目标：

1. 四主进程使用 NATS/hybrid 时，ready announce、peer poll 和 timeout 全部失败关闭；
2. 每次标准部署生成同一、非秘密、可审计的 runtime readiness generation；
3. ready key 与 payload 同时绑定 generation，旧代次不能满足新代次 barrier；
4. peer payload 必须匹配 role 与 generation，畸形/旧值按 missing 处理；
5. 在启动任何 background publisher 前完成 barrier，失败时不报告 ready；
6. monolith、in-memory 单元测试和明确无跨进程 peer 的路径保持兼容；
7. 退出时 best-effort 删除本角色本代次 key，减少残留窗口。

非目标：不改变 JetStream topic 分类/retention，不新增 decision outbox，不重构 EventBus，
不实现进程持续 liveness lease，不执行 NATS/Redis/Docker/WSL2 或 live 测试，不放开任何
live profile。

## 3. 用户与运行场景

- 标准四进程模拟部署：deploy 生成 generation，经 Compose 注入四个主进程；各进程先
  注册 NATS consumer、announce 本代次 ready，再等待同代次三个 peer 后启动 publisher；
- Redis 暂时不可用：announce/poll 抛错，进程启动非零或 Gateway lifespan 失败；
- peer 启动缓慢/失败：60 秒后当前进程失败，不启动 publisher；
- 旧部署 key 存在：因 generation 不同不能被读取或接受；
- 本地 monolith/in-memory：无跨进程 NATS/peer，不要求 generation，不访问 Redis barrier；
- 非标准手工四进程：缺 generation 时明确失败，不能静默退化；标准入口仍是 deploy script。

## 4. 当前路径与真源

```text
build_runtime
  -> start NATS bus + provision streams
  -> register role durable subscriptions
  -> _announce_runtime_ready fixed role key
       Redis error -> warning + return
  -> _wait_for_peer_roles_ready fixed peer keys
       Redis error/60s timeout -> warning + return
  -> runtime.start_background_tasks
       publishers may emit into INTEREST stream
```

真源：`aats/bootstrap/process_lifecycle.py`、`apps/api_gateway/main.py`、
`aats/bus/nats_bus.py`、`aats/bootstrap/settings.py`、`scripts/deploy.sh`、
`deploy/wsl2-dev/docker-compose.aats.yml` 与 readiness/deploy 测试。

## 5. 严格模式与失败关闭契约

严格 barrier 条件：当前 role 是 gateway/market/decision/execution，且有效
`event_bus_backend` 为 `hybrid` 或 `nats`。此时：

- generation 缺失或不合法：启动失败；
- `hot_state_store` 缺失：启动失败；
- announce Redis set 异常：启动失败；
- peer get_many 异常：启动失败；
- timeout 仍有 peer missing：启动失败；
- role/generation 不匹配的 payload 不算 ready。

`monolith`、无 peer 或 `in_memory` 路径不需要 generation。可选兼容调用仍可 warning/no-op，
但标准四进程编排必须显式传 `required=True`，不能依赖函数默认值绕过。

## 6. 部署代次契约

标准 deploy 在代码同步后、镜像构建前生成：

```text
<deployed-head-short>-<UTC timestamp>-<random nonce>
```

generation 不含凭证、账户或主机身份，长度不超过 128，只允许字母数字、点、下划线、
冒号和连字符。脚本通过受控 Compose environment 注入
`AATS_RUNTIME_READINESS_GENERATION`；Compose 对缺失值使用 required interpolation，
避免直接调用 Compose 时静默用空代次。

key 变为：

```text
aats:runtime:ready:<generation>:<role>
```

payload 同时保存 `generation`、`process_role`、`ready_ts`、`pid`。日志和模拟部署证据包可记录
generation，因为它不是秘密；不得记录 Redis URL 或任何 env 文件内容。

## 7. Key 生命周期与重启边界

ready key 继续使用 300 秒 TTL，表示“本代次已完成 consumer 注册”的启动事实，不是持续
业务健康 lease。优雅退出时只删除本角色/本 generation key；删除失败只告警，不覆盖原始
退出原因。崩溃残留最多受 TTL 限制。

同 generation 的单容器重启可能复用其他 peer 已完成 consumer provisioning 的启动事实；
durable consumer 可在 subscriber 临时离线时继续形成 INTEREST。peer 当前是否活着仍由
FS-006 critical task/容器监督负责，本阶段不把 ready key 伪装成持续 liveness。

## 8. API 与数据契约

不改变 HTTP API、事件 payload、数据库 schema、Redis 热状态业务 key 或 NATS subject。
新增一个启动环境设置和 generation-scoped 临时 key。

Gateway barrier 失败发生在 `app.state.runtime`、dashboard snapshot plane 和 HTTP ready
之前；daemon barrier 失败发生在 `runtime.start_background_tasks` 和 heartbeat ready 之前，
顶层返回非零。不得通过 HTTP 200/容器 heartbeat 报告成功。

## 9. 控制流与错误语义

```text
deploy sync
  -> generate readiness generation
  -> build/down/infra/schema
  -> compose up passes same generation

process build_runtime + subscriptions
  -> resolve strict requirement
  -> validate generation
  -> announce generation-scoped key
     failure -> raise RuntimeError; no publisher
  -> wait exact same-generation peer payloads
     Redis failure/timeout -> raise RuntimeError; no publisher
  -> start_background_tasks
  -> ready/heartbeat
  -> graceful stop: best-effort withdraw own key
```

异常对外只使用固定错误码/类型，不包含 Redis URL、payload 或凭证。日志可包含缺失 peer role
和非秘密 generation，便于定位部署代次错配。

## 10. 性能与容量

每个主进程启动阶段每 500ms 一批读取三个 key，最多 60 秒；稳态不轮询。generation 使
Redis key 数每部署最多四个，优雅退出删除，异常残留 300 秒后过期。没有新增长期 task、
线程或数据库连接。

60 秒是现有启动窗口，不是目标环境验收结果。若合法启动超过窗口，应先测量/解释并通过
受控配置设计调整，不能恢复 fail-open。

## 11. 日志、监控与审计

保留 announce/wait/all-ready 结构化事件；错误改为 startup failure，同时包含 role、
generation、missing peers、timeout 或 exception type。禁止记录 Redis URL 和异常正文中
可能包含的连接串；顶层既有 exception logger 的 `error=str(exc)` 只能接收本阶段固定
RuntimeError，不直接转发底层 Redis exception 文本。

本阶段不新增 Prometheus metric 或报警规则。目标 NATS/Redis 故障下的告警送达仍 OPEN。

## 12. 测试策略

1. generation key/payload/TTL 与不同代次隔离；
2. strict announce 的 no-store/set error 失败，optional in-memory 兼容；
3. strict wait 的 get error/timeout 失败，全部 peer 后成功；
4. payload role/generation 不符不能满足 barrier；
5. NATS split role 缺 generation 在 publisher/background task 前失败；
6. in-memory/monolith 不要求 generation；
7. Gateway lifespan barrier 失败不设置 runtime、不启动 dashboard；
8. deploy generation 在 sync 后/build 前生成，Compose required interpolation 并传四主进程；
9. 优雅退出 best-effort withdraw；
10. Ruff、readiness/deploy/process 相关回归、全量 unit、Markdown 链接与 diff check。

不运行 Redis/NATS integration 或 Compose；目标 startup/restart/failure matrix 保持未验证。

## 13. 迁移、回滚与兼容

标准 deploy 会自动生成 generation，无需修改 `.env.*`。直接手工 Compose 或手工四进程
启动若缺 generation 会从“可能继续”变为明确失败，这是预期安全收紧。

回滚不得只恢复 warning fallback；若 generation 注入本身故障，应修复标准 deploy/Compose
传递。真正回滚需同时回退应用严格读取、Compose required env 和 deploy 生成逻辑，且先将
`AATS_EVENTS` 恢复到经证明安全的 LIMITS 或提供等价消息保留控制。

## 14. 配置与环境隔离

新增 `AATS_RUNTIME_READINESS_GENERATION`：可选代码默认只服务 monolith/in-memory；
四主进程 NATS/hybrid 启动时必填。它由部署流水线生成，不写入 profile env、模板凭证或
长期配置文档。

本阶段不读取 `.env.wsl2`、`.env.*`，不修改 profile 身份、数据库 URL、NATS URL 或 Redis
URL。所有 live profile 继续由 FS-007 在任何部署副作用前禁用。

## 15. 代码组织与依赖

修改 `aats/bootstrap/process_lifecycle.py`、`apps/api_gateway/main.py`、
`aats/bootstrap/settings.py`、`scripts/deploy.sh`、
`deploy/wsl2-dev/docker-compose.aats.yml`、`scripts/write_deployment_evidence.py`、
相关测试和现行/审计文档。

只使用 Python/PowerShell/Bash 标准能力与现有依赖，不新增 package、数据库 migration 或
外部服务。

## 16. 最终裁定边界

本阶段验收后目标状态：

```text
CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN
```

关闭 FS-016 仍需在隔离生产等价四进程环境验证：新部署/重启/延迟启动、Redis 在 announce
前后断连、NATS consumer provisioning 延迟/失败、旧 generation key、peer crash/restart 和
INTEREST 消息计数；证明失败时无 publisher/无 ready、恢复后无关键状态缺口，并由独立
reviewer 复核。真实资金继续 NO-GO。
