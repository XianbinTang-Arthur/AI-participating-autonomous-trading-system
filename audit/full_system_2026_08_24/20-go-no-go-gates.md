# 20 真实资金上线 GO/NO-GO 门禁

> 日期：2026-08-24；基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`。  
> 当前决定：**NO-GO**。  
> 本文件定义“需要什么证据才能重新评估”，不是修复授权、部署指令或上线批准。

## 1. Finding 级分类

| Finding | Final Severity | 分类 | 当前状态 | 放行所需证据/补偿控制 |
|---|---:|---|---|---|
| `FS-002` | **P0** | **HARD BLOCKER** | PARTIAL / TARGET PARTITION-EXPIRY OPEN | generation/fence、不虚假 ack、最终竞态 outbound=0、验证后 reduce-only 与 15 秒同 generation permission 已有代码/隔离证据；仍需真实 Redis/NATS 四进程单向分区、服务器 TTL/kill -9/restart/告警、多实例和独立复核；详见 `21`、`32` |
| `FS-001` | P1 | **HARD BLOCKER** | PARTIAL / APPLY & ROLLBACK FAIL-CLOSED / RUNTIME ACTIVATION OPEN | profile apply/rollback 的虚假成功均已改为无写入 `501`；仍需 persisted active set、live authority/history 与 worker runtime 同 generation 读回后才能终态成功，部分失败必须 pending/failed，并对账历史 applied 行；详见 `22`、`33` |
| `FS-003` | P1 | **HARD BLOCKER** | CODE REMEDIATED / OPEN | `next_bar_event_v2`、显式时间线、gap/unfinished/no-liquidity/partial 回归已通过；仍需旧 artifact 清单、受影响策略重跑、独立 reviewer 复核，并由 FS-014 现实成交模型约束收益外推；详见 `23` |
| `FS-014` | P2 | **CONDITIONAL BLOCKER** | PARTIAL / OHLCV CONTAINED / L2 CALIBRATION OPEN | `ohlcv_participation_cap_v2` 已阻断无量全成并记录 fee+fixed slippage；仍需历史 L2/trades/真实 fill 的 spread/queue/latency/impact 校准、容量置信区间、旧证据重跑和独立复核；详见 `34` |
| `FS-004` | P2 | **CONDITIONAL BLOCKER** | PARTIAL / TEST SEALED FROM SELECTION / FINAL OOS & HISTORY AUDIT OPEN | v2 runner 已用 train/valid 双门、valid-only execution evidence，并把 test 限制为输入质量/来源检查与内容 seal；仍需 v1/v2 lineage/人工查看审计、独立一次性 holdout、walk-forward、多重检验、production gate 和 reviewer；若完全不用于 live 授权，需书面隔离证明；详见 `42` |
| `FS-005` | P2 | **CONDITIONAL BLOCKER** | CODE REMEDIATED / TARGET NETWORK OPEN | Gateway Compose/local launcher/evidence 已收口到 loopback；仍需目标 host 证明实际 HostIp、非授权网络不可达、HTTPS 强制、证书有效、HTTP auth 拒绝、Host/cookie/限流安全；远程访问只能经受控 VPN/proxy；详见 `27` |
| `FS-020` | P2 | **CONDITIONAL BLOCKER** | CODE & ASGI REMEDIATED / TARGET TLS-BROWSER OPEN | Host allowlist 和严格安全头已有代码/ASGI 证据；仍需目标 TLS/proxy/browser 证明 header 未降级、CSP 无 violation、HSTS/域名/证书一致，并验证未捕获 500 响应头；详见 `28` |
| `FS-019` | P2 | **CONDITIONAL BLOCKER** | CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD OPEN | 完整登录链有界 offload、取消安全 capacity、每进程三维限流和 dummy KDF 已有代码/隔离证据；仍需集中分布式限流、trusted proxy、真实 DB/慢连接、目标负载与紧急 Operator SLA；详见 `29` |
| `FS-016` | P2 | **CONDITIONAL BLOCKER** | CODE REMEDIATED / TARGET NATS STARTUP-RESTART OPEN | 同部署 generation strict barrier 和 deploy/Compose/evidence 传递已有代码/隔离证据；仍需真 Redis/NATS/Compose 新部署、重启、peer 延迟/失败、断连和 INTEREST 消息计数矩阵；详见 `30` |
| `FS-017` | P2 | **CONDITIONAL BLOCKER** | CODE REMEDIATED / TARGET BROWSER & ASSISTIVE-TECH OPEN | 原生 modal、初始/返回焦点和统一关闭路径已有静态/单元证据；仍需目标浏览器 keyboard-only、NVDA/VoiceOver、axe、缩放/窄屏和代表性 drawer 工作流；详见 `35` |
| `FS-018` | P3 | **RELEASE QUALITY GATE** | CODE REMEDIATED / TARGET REDUCED-MOTION OPEN | CSS/JS 已读取 reduce 偏好并停止非必要运动；仍需目标浏览器开关和视觉观察，不能仅凭源码标 CLOSED；详见 `35` |
| `FS-010` | P3 | **RELEASE QUALITY GATE** | CODE REMEDIATED / MANAGED UNKNOWN-KEY FAIL-CLOSED / TARGET STARTUP OPEN | 无消费者伪键已删除，managed YAML 非 mapping/unknown key 在 loader 失败关闭；仍需 committed candidate 四 profile 目标启动、仓库外 overlay 盘点、generator clean-run 与独立复核；详见 `36` |
| `FS-011` | P3 | **RELEASE QUALITY GATE** | CODE REMEDIATED / LEGACY ENTRY FAIL-CLOSED / EXTERNAL CALLER MIGRATION OPEN | 旧入口不再加载 profile/runtime 或抛签名 TypeError，固定迁移指引与 exit 2；仍需 committed candidate 独立复核和仓库外调用方迁移；详见 `37` |
| `FS-015` | P3 | **RELEASE QUALITY GATE** | CODE REMEDIATED / HISTORICAL EVIDENCE RE-RUN & INDEPENDENT REVIEW OPEN | replay 已用生产同名 strict boolean 在 history/dominant 前关闭 short，并有 golden vector；仍需按显式目标 gate 重跑历史 artifact、扩展其他 parity vector 与 committed candidate 独立复核；详见 `38` |
| `FS-006` | P1 | **HARD BLOCKER** | PARTIAL / OPEN | task exception/cancel/提前返回与七条固定周期任务永久 await/连续无成功周期已触发 daemon 非零退出或 FastAPI health `503`；仍需事件驱动任务的 connection/freshness/queue-lag、event-loop 外部监督、真实依赖/容器 restart/告警、停机上界与独立复核；详见 `24`、`31` |
| `FS-007` | P1 | **HARD BLOCKER** | RISK CONTAINED / LIVE DISABLED / OPEN | 无默认 profile、live 副作用前硬禁用、关键模拟步骤失败关闭和非 production-ready evidence 已实现；重新开放前仍需完整 trading-readiness packet、任一 unknown 非零、克隆部署矩阵与 app+schema+parameter 一致回滚；详见 `26` |
| `FS-008` | P2 | **CONDITIONAL BLOCKER** | PARTIAL / DECLARED TOPOLOGY 150 / TARGET LOAD & TRANSIENT PATHS OPEN | 角色预算、普通容量 197/名义余量 47 和 engine inventory 已有静态证据；仍需全 live daemon 容量/故障/重连/恢复-admin/瞬时路径/联合内存压测及报警阈值，不能把声明 ceiling 当运行峰值；详见 `41` |
| `FS-009` | P1 | **HARD BLOCKER** | PARTIAL / OPEN | 显式 root+RDP job、Batch B ledger/checksum/transaction 和 Gateway 失败关闭已有代码/隔离证据；仍需同 revision 完整 manifest 一致、真 PostgreSQL 前滚/部分失败/回滚、app+schema 一致恢复和独立复核；详见 `25` |
| `FS-021` | P2 | **RELEASE QUALITY GATE** | PARTIAL / BASE CI ADDED / REMOTE ENFORCEMENT OPEN | 只读 Python 3.12 Ruff/unit/strict-warning workflow 与 mock 修复已有代码/隔离证据；仍需远端 run、required check、integration/UI/Compose/schema/security gate 与 warning allowlist 清零；详见 `39` |
| `FS-022` | P2 | **RELEASE QUALITY GATE** | PARTIAL / LOCKS & DIGESTS ADDED / APT-SBOM-SCAN-CLEAN BUILD OPEN | Python 3.12/Linux runtime+CI lock/hash、Python base 与九个外部 image digest、防回退契约已有代码/制品校验证据；仍需 APT snapshot、clean build、SBOM、CVE/license/secret/signature/provenance、远端治理与独立复核；详见 `12`、`40` |

本轮 P1 集合中没有 `NON-BLOCKER`。三个降为 P2 的 finding 均与目标 live 环境或策略授权有关；只有在明确的隔离/补偿条件被证据化后，才可不独立阻断上线。

## 2. 系统级硬门禁

| Gate | 要回答的问题 | PASS 标准 | 当前裁定 |
|---|---|---|---|
| G0 安全验证环境 | 首次验证是否可能触达真实账户/订单？ | 隔离 exchange stub/demo、隔离 DB、凭证不输出；确认无 live-funds 写路径 | 未形成完整验证包 |
| G1 紧急停止权威性 | operator halt 是否让所有 execution worker 在有界时间停止新风险？ | ack/generation/fence 全部验证；Redis/NATS 故障、乱序、重启和提交竞态均 fail-closed | **PARTIAL / 未放行（FS-002/P0）**：代码/内存替身已将旧权限收紧为 15 秒 lease，真实 Redis/NATS 四进程单向分区、目标 TTL 时界、crash/restart 与多实例未证 |
| G2 参数变更真实性 | apply/rollback 的终态是否等于 runtime 有效状态？ | 持久层、热状态、worker 与 UI/API 读回一致；部分失败不返回成功 | **PARTIAL / 未放行（FS-001）**：apply/rollback 错误成功均已消除，但两个 endpoint 当前都不可用；真 profile activation/reverse saga、worker readback 与历史对账未实现 |
| G3 资本证据因果性 | 策略绩效是否仅使用当时可观察信息和可交易价格？ | 新时间契约、现实成交模型、旧证据失效、封存 OOS/独立复核 | **PARTIAL / 未放行（FS-003/004/014/015）**：same-bar 已阻断，OHLCV fill 已有 volume/cap/partial/fee+slippage containment，short-bias 关闭 gate 已与生产一致；Research Factory v2 已隔离 test selection 但最终 OOS/历史审计未完成；旧结果清单/重跑、真实 L2/queue/impact/latency 校准和独立复核仍未闭环 |
| G4 业务健康真实性 | 任何 critical worker 死亡能否被及时发现并安全收敛？ | crash/hang/lag 注入时 readiness 失败并 halt/退出；组件级 last-success 可读回 | **PARTIAL / 未放行（FS-006）**：task-exit 与七条固定周期任务的成功进度超时代码/隔离路径已非零/503；事件驱动任务、整体 event-loop stall、真实依赖/容器/告警未证 |
| G5 部署与恢复原子性 | 部署能否证明 trading-ready，并在失败时一致回退？ | 完整 readiness packet；显式 live；旧应用/参数/schema 回滚演练；任何 unknown 阻断 | **PARTIAL / 未放行（FS-007/016）**：标准 live 入口已硬禁用，模拟部署失败关闭/evidence 已加强，peer barrier 代码已 strict/同代次；production readiness、一致回滚与真 NATS/Redis 启动重启矩阵未完成 |
| G6 Schema 一致性 | 相同 revision 是否只有一个可验证 schema？ | manifest 全等；单一迁移入口；失败不被吞；fresh/upgrade/rollback/partial failure 全通过 | **PARTIAL / 未放行（FS-009）**：单一 deploy job、ledger/checksum 和启动失败关闭已实现；真 PG manifest/rollback 未证 |
| G7 生产边界与容量 | 网络、认证和 DB 是否有目标环境证据？ | 网络/TLS/认证/浏览器边界 PASS；集中限流不可绕过；生产等价容量与故障余量 PASS | **PARTIAL / 未放行（FS-005/019/020/008）**：loopback、Host、安全头、登录隔离和 DB 声明预算代码已收口；目标网络/TLS/proxy/browser、分布式 auth 限流、DB 负载/故障/瞬时路径与内存均未证 |
| G8 资金状态与对账 | 账户、订单、仓位、成交、账本与 recovery 是否 clean？ | 受控只读/标准健康证据，时间戳新鲜，reconciliation clean，无未知 command/outbox | 本轮未验证 |

**GO 判定规则：**所有 HARD BLOCKER 必须关闭并经独立人工复核；所有 CONDITIONAL BLOCKER 必须有可审计的补偿控制或关闭证据；所有系统 gate 必须为 PASS。`UNKNOWN` 与 `DEGRADED` 均按 NO-GO 处理，不能折算为“默认正常”。

## 3. Trading-readiness packet 最低内容

重新申请 GO 前，应由一个不可变、带时间戳的 packet 同时证明：

1. Git commit、构建 image digest、Compose profile、exchange mode 与目标环境一致；
2. root 与 RDP schema revision/manifest 一致，迁移无 pending/partial/吞错；
3. active parameter set、risk limits、mode、kill generation 已由 execution 进程读回；
4. Gateway、market、decision、execution、RDP 以及全部 live-only collectors 的 critical tasks 均 ready；
5. market/account/position 数据 freshness 在阈值内，recovery 与 reconciliation 为 clean；
6. command、outbox、pending order、unknown/ambiguous submission backlog 为零或有明确阻断状态；
7. Kill Switch 从控制面触发后，所有 execution worker 有界 ack，风险增加 outbound 为零，reduce-only 保持可用；
8. 目标网络只允许授权路径，TLS/证书/Host/auth/cookie/限流证据有效；
9. 数据库连接、内存、队列、任务 lag 在生产等价峰值与恢复场景下有足够余量；
10. 上一可用 app image、参数与 schema 的一致回滚已在克隆环境演练并记录 RTO/RPO；
11. 受影响策略的回测/验证采用因果正确的时间与成交模型，旧污染证据明确撤销；
12. 独立复核人签字确认静态验证、隔离运行验证和目标环境只读验证三者均有证据，且未知项为零。

## 4. 重新评估顺序

```text
P0 Kill Switch 控制闭环
  -> 真实 profile apply/rollback 与 runtime readback
  -> 因果正确的策略证据
  -> critical task supervision
  -> 单一 schema 与克隆库迁移演练
  -> 部署/readiness/一致回滚
  -> 目标网络与容量验证
  -> 账户/行情/订单/对账只读 readiness packet
  -> 独立人工复核
  -> 才能重新作 GO/NO-GO 决策
```

任一上游 gate 未通过时，不应以执行下游测试来制造“整体已接近可上线”的印象。尤其不得把容器 healthy、HTTP 200、单元测试通过、静态配置可解析或当前无订单，替代 execution halt ack、运行参数读回、schema identity、市场/账户 freshness 和 reconciliation clean。

## 5. 当前最终决定

当前至少有一个 P0 与五个 P1 HARD BLOCKER，且三个目标环境/历史行为门禁仍为 UNKNOWN。**真实资金生产继续 NO-GO。**

后续任何修复都应在独立任务中先完成设计与人工批准，再实施最小变更、隔离测试和独立复核。Phase 2 未授权或实施修复；后续 Phase 3A–3V 已实施对应审计记录中的代码/隔离收口，Phase 3S 新增 FS-021 基础 lint/unit/warning workflow，Phase 3T 新增 hashed Python locks 与外部 image digests，Phase 3U 新增 FS-008 角色化数据库连接预算和 engine inventory，Phase 3V 新增 FS-004 train/valid 选择与 test 内容封存；远端 required check、integration、安全扫描、完整供应链、目标容量、最终 OOS 与历史研究 lineage 仍开放。这些阶段都没有执行部署、真实账户操作或上线授权，证据与未验边界见 `21`–`42`。
