# 15 合并风险登记簿

> 基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`。2026-08-24 Phase 2 对抗复核确认 1 个 P0；Phase 3A–3V 已在工作分支实施但尚未提交 FS-002、FS-001、FS-003、FS-006、FS-009、FS-007、FS-005、FS-020、FS-019、FS-016、FS-014、FS-017、FS-018、FS-010、FS-011、FS-015、FS-021、FS-022、FS-008 与 FS-004 收口。运行与独立复核门禁仍未全部关闭，完整证据见 `21`–`42`。

| ID | 级别 | 状态 | 置信度 | 类别 | 核心证据/位置 | 最坏可信后果 |
|---|---|---|---|---|---|---|
| FS-001 | P1 | **PARTIALLY REMEDIATED / APPLY & ROLLBACK FAIL-CLOSED / RUNTIME ACTIVATION OPEN（Phase 3M）** | 高 | 参数变更/操作安全 | profile apply/rollback 均改为授权后无写入 `501`；17 direct+Saga、321 RDP/profile related、4317 全量 unit 通过；真 execution-owned activation/reverse saga/runtime readback 仍缺失 | 不再虚假报告 profile 已应用/回滚；但当前也无可用的 profile 运行参数变更/事故恢复能力，历史 applied 数据可能漂移 |
| FS-002 | **P0** | **PARTIALLY REMEDIATED / TARGET PARTITION-EXPIRY VERIFICATION OPEN（Phase 3L）** | 高（代码/隔离测试） | 下单/并发/跨进程控制 | generation ack + 最终 fence + Redis authority + 15s/5s generation permission + proxied resume activation；28 focused、225 related、4312 全量 unit 通过 | 原虚假 ack、最终竞态、恢复无许可窗口与无限期旧 RUNNING 许可代码路径已收紧；真 Redis/NATS 四进程单向分区、TTL 秒数、多实例和独立复核未证 |
| FS-003 | P1 | **CODE REMEDIATED / REVIEW & EVIDENCE RE-RUN OPEN（Phase 3C）** | 高（代码/隔离测试） | 回测/lookahead | `next_bar_event_v2` + 10 项对抗测试；26 focused、182 related、4161 全量 unit 通过；旧 artifact 清单/重跑与独立复核未完成 | 新运行不再同 bar close 成交；但旧收益证据仍失效，真实盘口现实性由 FS-014 继续阻断外推 |
| FS-004 | **P2** | **PARTIALLY REMEDIATED / TEST SEALED FROM CANDIDATE SELECTION / FINAL OOS & HISTORY AUDIT OPEN（Phase 3V）** | 中高（代码/隔离测试） | 研究/选择偏差 | real-data v2 采用 train/valid 双门；test 只做输入质量/来源检查与 seal；execution summary 必须精确绑定 valid；57 focused、327 related 通过 | 当前 v2 的 test 直接/全窗口执行指标间接选候选路径已阻断；历史 v1/人工查看、最终一次性 OOS、walk-forward、多重检验、下游 gate 与独立复核仍未知 |
| FS-005 | **P2** | **CODE REMEDIATED / TARGET NETWORK VERIFICATION OPEN（Phase 3G）** | 高（代码/隔离测试） | 网络/凭证 | Gateway mapping 固定 loopback；local launcher 禁止 live/非 loopback；evidence 校验实际 HostIp；76 related、4,219 unit 通过 | 仓库默认 all-interface 路径已阻断；现有容器、生产可达性、防火墙/VPN/NAT/TLS 仍未知 |
| FS-006 | P1 | **PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN（Phase 3K）** | 高（代码/隔离测试） | 进程监督 | task-exit supervisor + 七条固定周期任务成功进度 deadline/stalled；19 focused、118 related、4296 全量 unit 通过 | task 结束、纳管循环永久 await/连续无成功周期已阻断；事件驱动任务、event-loop 整体阻塞、真实依赖/容器 restart/告警仍未证 |
| FS-007 | P1 | **RISK CONTAINED / LIVE DISABLED / READINESS & CONSISTENT ROLLBACK OPEN（Phase 3F）** | 高（代码/隔离测试） | 部署/readiness | 无默认 profile、live 副作用前硬禁用、关键步骤非零中止、future collector 契约、模拟 evidence；73 项相关与 4,197 项全量 unit 通过 | 标准入口意外 live 已阻断；完整 production readiness、克隆部署与 app+schema+parameter 一致回滚仍未证明 |
| FS-008 | **P2** | **PARTIALLY REMEDIATED / DECLARED TOPOLOGY BUDGETED / TARGET LOAD & TRANSIENT PATHS OPEN（Phase 3U）** | 中高（静态预算） | DB容量 | 原 317/321；当前 14 组件声明 ceiling 150、普通容量 197、名义余量 47；13 engine calls 静态归类 | 目标负载/慢查询/重连/瞬时路径/内存/告警未验；不是运行时全局硬上限 |
| FS-009 | P1 | **PARTIALLY REMEDIATED / CLONE MANIFEST & ROLLBACK OPEN（Phase 3E）** | 高（代码/隔离测试） | schema治理 | root+RDP 显式 job + Batch B ledger/checksum/transaction；16 focused、220 related、4186 unit 通过；真 PG/clone 未跑 | 应用内隐式 DDL/吞错已阻断；但完整 manifest 等价与 app+schema rollback 仍不能证明 |
| FS-012 | P2 | **CODE PATH REMEDIATED / RUNTIME VERIFY OPEN（Phase 3E）** | 高（代码/隔离测试） | startup | Gateway 在 build/readiness/background 前只读 RDP validate，异常阻断 lifespan | 原“部分功能失效仍 ready”路径已阻断；真 Compose 运行仍未验 |
| FS-014 | P2 | **PARTIALLY REMEDIATED / OHLCV CONTAINED / L2 CALIBRATION OPEN（Phase 3N）** | 高（代码/隔离测试） | 成交现实性 | `ohlcv_participation_cap_v2` + volume/cap partial fill + fee/slippage 分解 + artifact 限制；119 related、4329 full unit 通过 | 无量全成与成本漏记已阻断；真实 L2/queue/spread/impact/latency、容量置信区间、旧证据重跑与独立复核仍缺失 |
| FS-016 | P2 | **CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN（Phase 3J）** | 高（代码/隔离测试） | NATS/readiness | generation-scoped strict barrier + deploy/Compose/evidence 代次；129 related、4,286 unit 通过 | 原 fail-open/旧 key 路径已阻断；真 Redis/NATS/Compose 启动重启断连与消息计数仍未证 |
| FS-017 | P2 | **CODE REMEDIATED / TARGET BROWSER & ASSISTIVE-TECH VERIFICATION OPEN（Phase 3O）** | 高（代码/静态测试） | 可访问性 | native modal dialog + trigger/initial/return focus + Escape/backdrop；8 focused、67 related、4337 full unit 通过 | 视觉-only 代码路径已消除；真实 keyboard/读屏/axe/缩放/移动 viewport 仍未证 |
| FS-019 | P2 | **CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN（Phase 3I）** | 高（代码/隔离测试） | Auth可用性 | 完整登录链有界 offload + cancel-safe capacity + 每进程 60/20/10 + dummy KDF；131 related、4,273 unit 通过 | event-loop 直接阻塞路径已消除；跨进程限流、慢 DB、目标容量与紧急 Operator SLA 仍未证 |
| FS-020 | P2 | **CODE & ASGI REMEDIATED / TARGET TLS-BROWSER VERIFICATION OPEN（Phase 3H）** | 高（代码/隔离测试） | 浏览器安全 | Host allowlist + strict CSP/frame/nosniff/referrer/permissions/COOP/CORP；HSTS 仅 HTTPS；44 focused、4,252 unit 通过 | 仓库缺失路径已阻断；真 TLS/proxy/browser 降级与最外层 500 仍未证 |
| FS-021 | P2 | **PARTIALLY REMEDIATED / BASE CI GATE ADDED / REMOTE ENFORCEMENT & INTEGRATION OPEN（Phase 3S）** | 高（代码/隔离测试） | 测试治理 | 只读 Python 3.12 Ruff/unit/strict-warning workflow；7 contract、17 focused、4375 full unit；远端 run/required check 未证 | 基础 lint/unit 新回归已有门禁定义；远端可绕过性、integration/security 缺口、SQLite allowlist 与 Python 3.14 资源兼容仍开放 |
| FS-022 | P2 | **PARTIALLY REMEDIATED / PYTHON LOCKS & IMAGE DIGESTS ADDED / APT-SBOM-CVE & CLEAN BUILD OPEN（Phase 3T）** | 高（代码/制品 hash 校验） | 供应链 | 46 runtime + 33 CI hashed distributions；Python base + 9 external image digests；6 contract、4381 full unit；APT/clean build/SBOM/scan 未证 | Python/tag 静默漂移路径已收紧；系统包、漏洞/许可证、来源证明、构建/集成兼容和远端治理仍未知 |
| FS-010 | P3 | **CODE REMEDIATED / MANAGED UNKNOWN-KEY FAIL-CLOSED / TARGET STARTUP VERIFICATION OPEN（Phase 3P）** | 高（代码/隔离测试） | 配置 | 伪 auto-rollback key 已删除；managed runtime defaults/YAML unknown key 与非 mapping 失败关闭；8 focused、56 related、4345 full unit 通过 | 原静默配置失真路径已消除；目标进程启动、仓库外 overlay、generator clean-run 与独立复核仍未证 |
| FS-011 | P3 | **CODE REMEDIATED / LEGACY ENTRY FAIL-CLOSED / EXTERNAL CALLER MIGRATION OPEN（Phase 3Q）** | 高（代码/隔离测试） | 旧入口 | 无 dotenv/runtime import 的迁移失败入口；6 focused、51 related、4351 full unit 通过 | TypeError/失败前配置注入已阻断；仓库外调用方与 committed candidate 独立复核仍未证 |
| FS-015 | P3 | **CODE REMEDIATED / HISTORICAL EVIDENCE RE-RUN & INDEPENDENT REVIEW OPEN（Phase 3R）** | 高（代码/隔离测试） | 回放/生产一致性 | 生产同名 strict boolean + history 前 gate + golden vector；17 focused、132 related、4368 full unit 通过 | 配置关闭后的 replay 开空路径已阻断；历史 artifact 未按显式 gate 重跑，其他输入/AI/成交 parity 与独立复核仍缺失 |
| FS-018 | P3 | **CODE REMEDIATED / TARGET REDUCED-MOTION VERIFICATION OPEN（Phase 3O）** | 高（代码/静态测试） | 可访问性 | CSS animation/transition/smooth scroll + JS scroll 读取 reduce 偏好；同组回归通过 | 缺失 media-query 代码路径已消除；目标浏览器开关和视觉结果仍未证 |

`FS-013` 保留为撤回项：首轮曾怀疑 `CLAIMED` submit 会在恢复时被自动重复提交；复核 command recovery、启动 blocker 和相关测试后，确认当前实现会 halt 并要求交易所对账，因此不登记为 finding，也不复用该编号。

## 优先级解释

- P1 必须在任何真实资金上线前关闭，并由独立复核人验证。
- P2 中 `FS-004/012/016/020/021/022` 应纳入同一上线整改批次；不能用“以后再做”接受不可证明的研究证据、readiness、测试或供应链状态。
- P3 可排入随后迭代；`FS-010/011/015` 的代码路径均已收口，但目标验证、外部配置/调用方盘点、历史证据重跑与独立复核仍需完成。

## 撤销/误报纪律

若后续证据证明 finding 不成立，应在登记簿标 `WITHDRAWN` 并保留原因，不能静默删除。若只证明默认配置暂不触发，应标 dormant，不应撤销结构性缺陷。
