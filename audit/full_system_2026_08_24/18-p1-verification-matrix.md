# 18 P1 对抗复核矩阵

> 日期：2026-08-24；基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`。  
> 本矩阵是 [17-p1-adversarial-verification.md](17-p1-adversarial-verification.md) 的决策索引，不能脱离完整证据单独用作上线批准。

## 1. 裁定矩阵

| Finding | Original Severity | Final Severity | Status | Confidence | Capital Risk | Runtime Test Needed |
|---|---:|---:|---|---|---|---|
| `FS-002` | P1 | **P0** | **UPGRADED** | 高 | halt 可被 API 报告成功而 execution 未停；已过早期门禁的风险增加订单还可跨过最终提交窗口 | **是，最高优先级**：隔离四进程、双传播故障、generation/ack、提交点故障注入 |
| `FS-001` | P1 | P1 | CONFIRMED | 高 | 参数未回退却形成成功/终态认知，错误参数继续驱动 live 决策 | 是：隔离 DB + worker readback + 部分失败 saga |
| `FS-003` | P1 | P1 | CONFIRMED | 高 | 因果错误污染收益/回撤/成交证据，可能让不合格策略获得真实资本 | 是：带明确 observation/decision/tradable timestamp 的 golden replay |
| `FS-004` | P1 | **P2** | **PARTIALLY REMEDIATED / TEST SEALED FROM SELECTION / FINAL OOS OPEN** | 中高 | v2 已阻断 test 直接形成 candidate，但历史 v1/人工查看与最终统计证据仍未知 | 是：只读 artifact/lineage 审计 + 独立一次性 OOS/walk-forward |
| `FS-005` | P1 | **P2** | **DOWNGRADED** | 高 | all-interface bind 扩大控制面攻击面；直接资金影响还需网络可达与认证失陷等条件 | 是：目标网络、TLS、证书、HTTP 拒绝、cookie 与防火墙验证 |
| `FS-006` | P1 | P1 | CONFIRMED | 高 | 关键执行/账户/对账组件可静默死亡，容器仍绿色，降险与状态正确性不可证明 | 是：逐 critical task crash/hang/依赖断连测试 |
| `FS-007` | P1 | P1 | CONFIRMED | 高 | 部署可在 profile/服务/schema/状态未知时被当成成功，失败后无一致回滚 | 是：生产等价克隆环境的部署与回滚故障矩阵 |
| `FS-008` | P1 | **P2** | **PARTIALLY REMEDIATED / DECLARED TOPOLOGY BUDGETED / TARGET LOAD & TRANSIENT PATHS OPEN** | 中高（静态预算） | 原 317/321；Phase 3U 声明 topology 150、普通容量 197、名义余量 47，但是否在目标负载/故障/瞬时叠加下耗尽未实测 | **是**：全 live daemon 的负载、慢查询、重启、恢复/admin、瞬时路径与联合内存测试 |
| `FS-009` | P1 | P1 | CONFIRMED | 高 | 同 revision 可形成不同 schema；表存在/服务绿色不能证明约束、ALTER、VIEW 完整 | 是：空库/历史克隆/部分失败库的 schema manifest 与回滚演练 |

## 2. 证据类型矩阵

| Finding | 静态全路径 | 反证/补偿控制 | 隔离动态复现 | 当前只读运行态 | live/交易所验证 |
|---|---|---|---|---|---|
| `FS-002` | 已完成 | 已完成 | 已完成：传播失败、最终提交竞态 | 不适用 | 未执行，且不得作为首次验证 |
| `FS-001` | 已完成 | 已完成 | 已完成：apply/rollback 零写入失败关闭；真实双库/worker activation 未完成 | 不适用 | 未执行 |
| `FS-003` | 已完成 | 已完成 | 现有测试证明当前成交契约；新时间契约未验证 | 不适用 | 不需要真实交易所即可验证 |
| `FS-004` | 已完成 | 已完成（Phase 3V train/valid 双门与 test seal） | 已完成内存隔离路径；最终 OOS 未完成 | artifact/registry 历史未审计 | 不需要真实交易所；需要独立历史数据与 artifact 流程 |
| `FS-005` | 已完成 | 已完成 | 未完成 | 模拟栈 all-interface + HTTP health 已观察 | 目标生产网络未验证 |
| `FS-006` | 已完成 | 已完成 | 已完成：business dead + heartbeat alive | 不适用 | 不需要真实交易所 |
| `FS-007` | 已完成 | 已完成 | 仅完成 shell 语法检查 | 未部署 | 未执行；应先用克隆环境 |
| `FS-008` | 已完成 | 已完成（Phase 3U 角色预算与 inventory） | 未完成 | 声明 ceiling 150、普通容量 197、名义余量 47；历史采样 40 | 生产等价峰值、故障/瞬时叠加、告警与内存未验证 |
| `FS-009` | 已完成 | 已完成 | 已完成：吞错 ready、ledger/checksum/order/transaction/rollback 替身 | 原只读运行快照不等于新 schema 等价 | 真 PostgreSQL/克隆库演练未执行 |

## 3. 严重度变化理由摘要

### 升级

- `FS-002`：首轮只证明 adapter 内部 TOCTOU；二审证明 API 不等待 execution ack，Redis/NATS 同时失败被吞后 execution 可无限期维持旧状态。两条路径叠加后，不再只是“可能多发一笔”，而是紧急停机控制可失效并继续风险增加交易。

### 降级

- `FS-004`：Phase 2 因当前 runner 没有训练、参数搜索、自动排名或自动 live 晋升而降级；Phase 3V 又修复 v2 test 直接选候选路径，但历史污染、最终 OOS 与统计治理仍开放，故保持 P2 部分整改。
- `FS-005`：all-interface bind 成立；live 部署同时强制 TLS/auth/Secure cookie。当前 HTTP 观测来自 simulated profile，不能外推为生产明文登录。
- `FS-008`：原静态硬上限确实超过 PostgreSQL 容量，但 overflow 是按需连接。Phase 3U 已将当前声明 topology ceiling 收敛为 150、名义余量 47；该值不是 runtime 全局 cap，且目标负载、故障重连、瞬时路径与内存仍缺实测，故保持 P2 部分整改而非 CLOSED。

## 4. 解释纪律

- `DOWNGRADED` 不等于已修复、可忽略或已通过 live 门禁。
- `CONFIRMED` 不代表已经在真实账户复现；静态或隔离故障注入足以证明结构性路径时，不应使用真实资金做验证。
- 任何运行测试都必须区分“测试通过”“目标生产环境状态已验证”和“真实交易安全”；三者不能互相替代。
- 本轮未读取 `.env.*` 内容，未查询余额/订单/持仓，未向交易所提交任何请求。

## 5. Phase 3C FS-003 追加状态

本矩阵第 1–2 节冻结 Phase 2 裁定。Phase 3C 后，FS-003 的新时间契约已经通过静态追踪和隔离动态复现：`next_bar_event_v2`、显式 observation/decision/submit/fill timeline、gap/unfinished/no-liquidity/partial/terminal 用例均通过，原 same-bar-close 复现从 1 fill 变为 0 fill。当前状态更新为 `CODE REMEDIATED / REVIEW & EVIDENCE RE-RUN OPEN`；旧 artifact 清单/策略重跑、独立 reviewer 与 FS-014 现实成交模型未完成，故 G3 仍为 PARTIAL。见 [23-fs-003-backtest-causal-timing-remediation.md](23-fs-003-backtest-causal-timing-remediation.md)。

## 6. Phase 3D FS-006 追加状态

本矩阵第 1–2 节冻结 Phase 2 裁定。Phase 3D 后，FS-006 的 task-exit 路径已经
通过静态追踪和隔离动态复现：原 `business dead + heartbeat alive + exit 0`
变为无需外部 stop 的 `exit 1`，heartbeat 停止；exception、cancel、提前返回和
FastAPI health `503` 均有断言。当前状态更新为
`PARTIALLY REMEDIATED / HANG-LAG RUNTIME VERIFICATION OPEN`；永久 hang、
last-success/lag、依赖断连、真实四进程容器 restart/告警和独立 reviewer 未完成，
故 G4 仍为 PARTIAL。见
[24-fs-006-critical-task-supervision-remediation.md](24-fs-006-critical-task-supervision-remediation.md)。

## 7. Phase 3E FS-009 追加状态

本矩阵第 1 节的 `CONFIRMED` 保留 Phase 2 原始 finding。Phase 3E 后，应用启动不再自行迁移 schema：部署在 app up 前运行一次性 root+RDP job，RDP 13 个 Batch B stage 具有 version/checksum/advisory-lock/predecessor/transaction/rollback-suffix contract；managed profile 只读验证，Gateway 在 build/readiness/background 前失败关闭。16 项 focused、220 项相关回归和 4,186 项全量 unit 通过。

当前状态更新为 `PARTIALLY REMEDIATED / CLONE MANIFEST & ROLLBACK OPEN`。新 PostgreSQL integration 及相关独立 SQL integration 共 7 项仅完成收集、未开启隔离 DB 运行；空库/历史克隆/部分失败的完整 manifest 全等和 app+schema rollback 未证，故 G6 仍为 PARTIAL 且不放行。见 [25-fs-009-schema-single-truth-remediation.md](25-fs-009-schema-single-truth-remediation.md)。

## 8. Phase 3F FS-007 追加状态

本矩阵第 1–2 节保留 Phase 2 原始 finding。Phase 3F 后，标准部署无默认 profile，三个 live profile 在任何副作用前失败且无 override；关键模拟部署步骤不吞错，future live collector 清单完整，模拟 evidence 明确不是 production/trading ready。11 项独立 FS-007、34 项 deploy focused、73 项扩大相关和 4,197 项全量 unit 通过。

当前状态更新为 `RISK CONTAINED / LIVE DISABLED / READINESS & CONSISTENT ROLLBACK OPEN`。没有执行 WSL2/Docker/克隆 DB/交易所；账户/行情、Kill Switch、活动参数、recovery/reconciliation、网络/容量和一致 rollback 均未证，故 G5 为 PARTIAL 且不放行。见 [26-fs-007-deployment-fail-closed-remediation.md](26-fs-007-deployment-fail-closed-remediation.md)。

## 9. Phase 3G FS-005 追加状态

本矩阵第 1–2 节保留 Phase 2 修复前 finding。Phase 3G 后，Gateway 宿主 mapping 固定 loopback，本地 launcher 禁止 live/非 loopback，模拟 evidence 校验实际 published HostIp；76 项 related 与 4,219 项全量 unit 通过。

当前状态更新为 `CODE REMEDIATED / TARGET NETWORK VERIFICATION OPEN`。没有容器重建/inspect 或网络探测；防火墙、LAN/VPN/NAT、TLS/证书、Host/auth/cookie/限流仍未证，故 FS-005 保持 P2、G7 不放行。见 [27-fs-005-gateway-loopback-containment.md](27-fs-005-gateway-loopback-containment.md)。

## 10. Phase 3H FS-020 追加状态

FS-020 不属于本矩阵的九项 P1 集合；本节仅追加它与 FS-005/G7 共享的浏览器/网络边界状态，不改写 Phase 2 矩阵。Phase 3H 已实施本机 Host allowlist、畸形/不信任 Host 400、严格 CSP/frame/nosniff/referrer/permissions/COOP/CORP 和 HTTPS-only HSTS。44 项 focused 与 4,252 项全量 unit 通过。

当前状态为 `CODE & ASGI REMEDIATED / TARGET TLS-BROWSER VERIFICATION OPEN`。没有目标 TLS/proxy/browser 证据，未捕获 500 响应头也没有故障注入；故 FS-020 保持 P2、G7 不放行。见 [28-fs-020-browser-security-headers-remediation.md](28-fs-020-browser-security-headers-remediation.md)。

## 11. Phase 3I FS-019 追加状态

FS-019 不属于本矩阵的九项 P1 集合；本节只更新它与 FS-005/020/008 共享的 G7
生产边界和容量状态。Phase 3I 已实施完整同步登录链有界 offload、取消安全的 capacity、
每进程三维 limiter、dummy KDF、输入与 hash iteration 上界。131 项扩大相关和 4,273
项全量 unit 通过。

当前状态为 `CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN`。
没有多进程集中限流、trusted proxy、真实 DB、慢连接、目标容量或告警证据；故 FS-019
保持 P2、G7 不放行。见
[29-fs-019-operator-login-async-isolation.md](29-fs-019-operator-login-async-isolation.md)。

## 12. Phase 3J FS-016 追加状态

FS-016 不属于本矩阵的九项 P1 集合；本节只更新它与 FS-006/007 共享的 startup/readiness/G5 边界。Phase 3J 已实施四主进程 NATS/hybrid 同部署 generation strict barrier，Redis 写/读异常、peer timeout、payload 错配、缺 generation/hot-state 都不启动 publisher。标准 deploy/Compose/evidence 也绑定该代次。129 项扩大相关和 4,286 项全量 unit 通过。

当前状态为 `CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN`。没有真 Redis/NATS/Compose 启动、重启、断连、consumer provisioning 延迟和消息计数证据；故 FS-016 保持 P2、G5 不放行。见 [30-fs-016-nats-peer-readiness-remediation.md](30-fs-016-nats-peer-readiness-remediation.md)。

## 13. Phase 3K FS-006 成功进度监督追加状态

本矩阵第 1–2 节继续冻结 Phase 2 原始 finding。Phase 3K 在 task-exit supervisor 上增加七条固定周期关键循环的成功进度 deadline：账户刷新、执行同步、对账、execution outbox、execution command flow、Phase 1 shadow 和 trial guard 永久 pending 或连续无成功周期时会分类为 `stalled`，daemon 非零退出或 FastAPI health `503`。19 项 focused、118 项扩大相关和 4,296 项全量 unit 通过。

当前状态更新为 `PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN`。WebSocket、decision dispatcher、abort hook、guard publisher 等事件驱动/service-owned task 尚无统一且可靠的 connection/freshness/queue-lag 契约；整体 event-loop stall、真依赖、容器 restart/告警、停机上界与独立 reviewer 也未完成，故 FS-006 保持 P1、G4 不放行。见 [31-fs-006-critical-task-progress-watchdog.md](31-fs-006-critical-task-progress-watchdog.md)。

## 14. Phase 3L FS-002 短时交易许可追加状态

本矩阵第 1–3 节继续冻结 Phase 2 原始 finding 和升级理由。Phase 3L 在 Phase 3A
generation/ack/final fence 之上，把长期恢复 state 与在线增险 permission 分离：
Gateway/monolith 维护同 generation、Redis TTL 15 秒、5 秒续租的 permission；
execution 最终 fence 必须读取且不能自行续租。双传播和 permission 删除同时失败的
InMemory 故障注入证明旧 permission 到期后拒绝新风险；四进程代理 resume 必须由
Gateway 激活同 generation permission 后才返回成功。28 项 focused、225 项扩大相关
和 4,312 项全量 unit 通过。

当前状态更新为 `PARTIALLY REMEDIATED / TARGET PARTITION-EXPIRY VERIFICATION OPEN`。
真实 Redis/NATS 四进程单向分区、服务器 TTL 秒数、kill -9、目标 restart/告警、
多实例 membership/ack、新旧 revision 混跑和独立 reviewer 未完成，故 FS-002 保持
P0、G1 不放行。见
[32-fs-002-short-lived-trading-permission-lease.md](32-fs-002-short-lived-trading-permission-lease.md)。

## 15. Phase 3M FS-001 Profile Apply 失败关闭追加状态

本矩阵第 1–3 节继续冻结 Phase 2 原始 finding。Phase 3B 已先封闭 rollback 虚假成功；
Phase 3M 进一步证明旧 apply Saga 的 `profile_id` 查询、非 schema threshold 写入和
profile active-set 行都没有目标 worker generation/readback 契约，因此 SQL 四步完成不能
形成 `applied/ok=true` 终态。当前 apply/rollback 都在 token/actor、状态与双签检查后
无写入 `501`，route 不再调用历史 Saga。17 项 direct+Saga、321 项 RDP/profile 扩大
相关与 4,317 项全量 unit 通过。

当前状态更新为 `PARTIALLY REMEDIATED / PROFILE APPLY & ROLLBACK FAIL-CLOSED /
RUNTIME ACTIVATION OPEN`。没有真实 execution-owned activation/reverse saga、worker
ack/readback、历史 applied 数据对账、真 Postgres/多进程故障注入或独立 reviewer，故
FS-001 保持 P1、G2 不放行。见
[33-fs-001-profile-apply-fail-closed.md](33-fs-001-profile-apply-fail-closed.md)。

## 16. Phase 3N FS-014 OHLCV 成交现实性收敛追加状态

FS-014 不属于本矩阵的九项 P1 集合；本节更新它与 FS-003 共享的 G3 资本证据
状态。Phase 3N 已固定 `ohlcv_participation_cap_v2`：IOC/post-only/bounded-limit
全部要求正 volume 并受默认 1% cap；IOC/bounded 只消费 observation volume，
bounded 采用 taker fee + fixed slippage，cost/scorecard 显式保留分项和 OHLCV/L2
限制。119 项相关回归和最终 4,329 项全量 unit 通过。

当前状态为 `PARTIALLY REMEDIATED / OHLCV CONTAINED / L2 CALIBRATION OPEN`。
没有真实 L2/trades/fill 校准、spread/queue/impact/latency、容量置信区间、旧 artifact
重跑或独立 reviewer；故 FS-014 保持 P2 条件阻断，FS-003/014 对应 G3 仍不放行。
见 [34-fs-014-ohlcv-fill-realism-containment.md](34-fs-014-ohlcv-fill-realism-containment.md)。
