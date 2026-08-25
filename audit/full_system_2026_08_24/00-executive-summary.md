# 00 执行摘要

## 上线判断

**结论：NO-GO。当前基线不应进入真实资金上线。**

原始基线的阻断原因不是“测试没跑”，而是存在可由代码与隔离故障注入证明的 P0/P1 问题：Kill Switch、参数真实性、回测因果时间、关键任务监督、部署 readiness 和 schema 单一真源均有缺口。Phase 3A–3V 工作区已部分或代码级收口 FS-002/001/003/006/009/007/005/020/019/016/014/017/018/010/011/015/021/022/008/004；FS-015 的 replay short-bias 关闭语义现与生产一致，FS-021 已有基础 lint/unit/warning workflow，FS-022 已加入 Python hashed locks 与外部 image digests，FS-008 已建立角色化连接预算和 engine inventory，FS-004 已把 real-data v2 的 test 从 candidate selection 隔离。但多个目标环境、真实分区、runtime activation、旧证据重跑、L2 校准、克隆库、网络/TLS、负载、瞬时数据库路径、辅助技术、远端 CI 治理、APT/SBOM/扫描、外部配置/调用方、最终 OOS、历史研究 lineage 与独立复核门禁仍未关闭，因此当前仍是 NO-GO。

> Phase 2（2026-08-24）已对首轮九个 P1 逐项反证：`FS-002` 升为 P0；`FS-004/005/008` 降为 P2；其余五项保持 P1。完整证据见 [17-p1-adversarial-verification.md](17-p1-adversarial-verification.md)。降级不表示已修复，相关历史行为/目标环境仍需验证。

> Phase 3 工作区更新：FS-002 已做 generation/ack/final fence，并在 Phase 3L 增加 generation-scoped 15 秒 permission lease，但真实 Redis/NATS 四进程单向分区、目标到期时界与独立复核仍 OPEN；FS-001 已在 Phase 3B/3M 消除 profile rollback/apply 虚假成功，两个入口均无写入失败关闭，但真实 activation、reverse saga、worker readback 与历史漂移对账仍 OPEN；FS-003 已用 `next_bar_event_v2` 阻断 same-bar 成交；Phase 3N 又用 `ohlcv_participation_cap_v2` 收敛无量全成、bar-volume 容量和固定滑点漏记，但旧策略证据重跑、独立复核与 L2/queue/impact 校准仍 OPEN；FS-006 已阻断关键 task 结束后的假健康，并为七条固定周期关键循环加入成功进度 deadline/stalled 失败路径，但事件驱动任务、整体 event-loop stall、真实依赖/容器行为仍 OPEN；FS-009 已建立显式 root+RDP schema job、完整 Batch B ledger/checksum 和 Gateway 启动失败关闭，但真 Postgres 克隆 manifest 与 app+schema rollback 仍 OPEN；FS-007 已取消默认 profile、硬禁用 live 并让关键模拟部署步骤失败关闭，但完整 readiness/一致回滚仍 OPEN；FS-005 已固定 Gateway loopback 并收紧本地入口，但目标网络仍 OPEN；FS-020 已加入 Host 失败关闭与严格浏览器安全头，但目标 TLS/proxy/browser 仍 OPEN；FS-019 已将同步登录链移出 event loop 并增加有界 worker/每进程限流/dummy KDF，但分布式限流和目标负载仍 OPEN；FS-016 已将 NATS/hybrid peer barrier 收紧为同代次失败关闭，但目标 Redis/NATS/Compose 启动重启矩阵仍 OPEN；Phase 3O 已把 FS-017/018 的详情 drawer 改为原生 modal/focus-return 并补 reduced-motion CSS/JS，但目标浏览器、键盘/读屏/axe/缩放/动效观察仍 OPEN；Phase 3P 已删除 FS-010 无消费者伪键，并在 managed loader 对非 mapping/unknown key 失败关闭，但目标启动、仓库外 overlay 与独立复核仍 OPEN；Phase 3Q 已将 FS-011 旧入口改为无配置副作用的迁移失败入口；Phase 3R 已让 FS-015 replay 使用同名 strict boolean gate 并在关闭时钳制 short score，但历史 artifact 重跑与独立复核仍 OPEN；Phase 3S 已新增只读 Python 3.12 lint/unit/warning workflow并修复 Long/Short 错误 mock；Phase 3T 已新增目标平台 Python hashed locks、Python base 和九个外部 image digests，但 APT、clean build、SBOM/扫描、远端 CI 与独立复核仍 OPEN；Phase 3U 已把 FS-008 四进程声明连接 ceiling 从历史 317/321 收敛为 150，普通容量 197、名义余量 47，并加入 engine inventory，但目标负载、瞬时路径、故障重连、内存与独立复核仍 OPEN；Phase 3V 已让 FS-004 real-data v2 使用 train/valid 双门，把 test 限制为输入质量检查与内容 seal，并阻断全窗口 execution summary 间接参与绩效选择，但最终 OOS、历史 v1/人工查看审计、walk-forward、多重检验与独立复核仍 OPEN。详见 `21`–`42`。本摘要下方“已验证事实”保留 Phase 1/2 原始故障快照，不代表修复后当前代码仍保持原行为。

## 风险概览

| 等级 | 数量 | 含义 |
|---|---:|---|
| P0 | 1 | `FS-002`：紧急停止传播与最终提交控制不能保证 execution 停止新风险 |
| P1 | 5 | 上线硬阻断，涉及错误 rollback、验证失真、假健康、部署 readiness 和 schema 一致性 |
| P2 | 11 | 三项由首轮 P1 降级；仍含运行验证、恢复、安全纵深、容量、测试可信度和可访问性风险 |
| P3 | 4 | 维护性、配置诚实性、旧入口和体验问题 |
| P4 | 0 | 未登记纯样式类问题 |

数量以 [15-consolidated-risk-register.md](15-consolidated-risk-register.md) 为准。

## 最重要的已验证事实

1. `FS-001`：原始 `POST /profile-recommendations/{id}/rollback` 只把推荐状态改成 `rolled_back` 并返回 `ok: true`；进一步复核又证明旧 apply Saga 的 SQL 完成不能证明 runtime 生效。Phase 3B/3M 已把 apply/rollback 都改为授权后无写入 `501`；真实双向 activation/readback 仍未实现。
2. `FS-002`：Phase 2 证明原 halt API 不等待 execution ack，Redis/NATS 故障可让 execution 无限期保持旧 RUNNING，且 adapter 最终提交前不复查。Phase 3A 已用同 generation execution ack 与最终 fence 阻断原利用；Phase 3L 又要求同 generation 15 秒 permission，InMemory 故障注入证明双传播/删除失败后旧 permission 会到期。真实 Redis/NATS 四进程单向分区和目标秒数仍未验证，所以保持 P0 OPEN。
3. `FS-003`：原始 harness 使用完整当前 bar 形成决策并以同一 close 成交；Phase 3C 已固定 `next_bar_event_v2` 并阻断 same-bar 路径。旧 artifact 尚未盘点/重跑，独立复核仍缺失。
4. `FS-014`：原始 IOC/bounded 无量全成、post-only 命中全成，成本诊断漏记成交价滑点。Phase 3N 已固定 `ohlcv_participation_cap_v2`，三类订单受 volume/默认 1% cap，成本计 fee+fixed slippage 且 artifact 明示 OHLCV 限制；L2/queue/spread/impact/latency 校准仍未完成。
5. `FS-004`：原研究工厂构造 train/valid/test，但候选指标和推荐只使用 test；当前函数未发现训练、自动搜索、排名或自动 live promotion，Phase 2 降为 P2。Phase 3V 的 v2 runner 已改为 train/valid 双门，test 只参与输入质量/来源检查与内容 seal；最终 OOS、历史重复人工适配与 artifact lineage 仍需审计。
6. `FS-005`：原始 Gateway Compose 映射没有 `127.0.0.1` 限定，当前模拟快照曾验证为所有宿主接口绑定。Phase 3G 已固定 loopback、本地 launcher 禁止 live/非 loopback，并让模拟 evidence 拒绝 runtime binding drift；现有容器、目标宿主网络、防火墙、VPN/NAT 与证书边界仍为 UNKNOWN。
7. `FS-006`：原始基线的三个主 daemon 健康检查只观察独立心跳 task；Phase 3D 已让显式关键 task 结束触发 heartbeat 停止与非零退出/health `503`，Phase 3K 又为七条固定周期关键循环增加成功进度 deadline，使永久 await/连续无成功周期分类为 `stalled`。事件驱动任务、整体 event-loop 阻塞、真实依赖/容器 restart/告警尚未验证。
8. `FS-007`：原始部署默认 profile 是 `derivatives-live`，且可吞关键步骤失败。Phase 3F 已取消默认 profile、硬禁用全部 live 标准入口、补齐 future live collector 契约并生成明确非 production-ready 的模拟 evidence；账户/行情新鲜度、对账、恢复、Kill Switch、活动参数、克隆部署与一致回滚仍未验证，G5 不放行。
9. `FS-008`：原基线全池稳态理论上限约 317（瞬时 321），超过 PostgreSQL 200；按角色估计的可信峰值约 142–160，Phase 2 模拟采样 40。Phase 3U 当前工作区已建立角色化预算和 engine inventory，声明 topology ceiling=150、普通容量=197、名义余量=47；生产目标负载、故障重连、瞬时路径和联合内存仍未实测，故保持 P2 部分整改。
10. `FS-009`：原始基线的 RDP 启动只做 `create_all`，Batch B ALTER/VIEW/CHECK 只能手工执行，Gateway 吞异常。Phase 3E 已收口为部署期显式 root+RDP job、Batch B ledger/checksum/order/transaction contract 和应用只读校验；真 Postgres fresh/upgrade/partial failure/rollback 及完整 manifest 未执行，不得标为 CLOSED。
11. `FS-020`：原始基线缺少 Host allowlist 和浏览器安全响应头。Phase 3H 已在路由前失败关闭非本机/畸形 Host，统一覆盖严格 CSP/frame/nosniff/referrer/permissions/COOP/CORP，HSTS 仅限实际 HTTPS scope；真 TLS/proxy/browser 和最外层 500 仍未验证。
12. `FS-019`：原始 async 登录在 event loop 直接执行同步 DB/PBKDF2/审计。Phase 3I 已完整 offload 到有界 worker，补每进程三维限流、dummy KDF 和输入/hash 上界；多进程集中限流、真实 DB、慢连接与目标 p95/p99/event-loop lag 仍未验证。
13. `FS-016`：原始 NATS/hybrid peer readiness 在 Redis 异常或 60 秒超时后继续 publisher，且固定 key 可命中旧部署事实。Phase 3J 已改为 generation-scoped 失败关闭并将代次记入模拟部署 evidence；真 Redis/NATS/Compose 启动、重启、断连和消息计数仍未验证。
14. `FS-017/018`：原始详情 drawer 只有 `<aside aria-hidden>` 与视觉遮罩，四类无限动画不读 reduced-motion。Phase 3O 已改为原生 modal、显式初始/返回焦点、统一 Escape/backdrop/按钮关闭，并让 CSS/JS 动效读取 reduce 偏好；目标浏览器、keyboard-only、读屏、axe、缩放和视觉观察仍未验证。
15. `FS-010`：原始四个 managed YAML 和现行文档声明一个没有 Settings 字段或消费者的 auto-rollback 键，运行时静默忽略。Phase 3P 已删除伪键，并让 managed runtime defaults/YAML 对非 mapping 与 unknown key 失败关闭；目标 profile 启动、仓库外 overlay、generator clean-run 和独立复核仍未验证。
16. `FS-011`：原始 `run_local.py` 在加载 profile 后按旧协程签名调用当前同步 decision process，必然 TypeError。Phase 3Q 已改为不加载 dotenv/runtime 的迁移失败入口，固定 stderr 指引与 exit 2；仓库外调用方迁移和独立复核仍未完成。
17. `FS-015`：原始 independent replay 无视生产 `strategy_short_bias_enabled=false`，仍可能选择 short dominant leg。Phase 3R 已把同名 strict boolean 写入 replay 参数/artifact，并在 history/dominant/state machine 前把 short score 钳制为 0；历史 artifact 重跑和 committed candidate 独立复核仍未完成。
18. `FS-021`：原始仓库没有 CI，Ruff 非零且 unit warning 噪声未治理。Phase 3S 已新增只读 Python 3.12 Ruff/unit/strict-warning workflow，修复 Long/Short 的错误 AsyncMock；远端执行、required check、integration/security gate 与 warning allowlist 清零仍未完成。
19. `FS-022`：原始发布/CI 依赖开放解析且镜像只有 tag。Phase 3T 已加入 46 项 runtime、33 项 CI 的精确版本/hash lock，固定 Python base 与九个外部 image digest；APT、clean build、SBOM、CVE/license/secret/provenance、远端治理与独立复核仍未完成。

## 已确认的良好控制

- live/simulated profile、OKX demo/live、submission enable、allowed symbols、notional/open-order 等有多层门禁。
- kill switch 明确允许 reduce-only/close 路径，符合降低敞口原则。
- Execution command 恢复对 `CLAIMED`/`SENT` 模糊状态采取停机与人工对账，而不是盲目重提。
- 成交、组合快照、fill outcome 与 outbox 存在原子事务和幂等键；组合恢复可从持久化成交重建。
- 认证使用 PBKDF2、HMAC session、HttpOnly/Secure cookie 默认值、账户锁定与数据库行锁。
- Research DSL 使用 AST 白名单并拒绝未来 Ref 偏移；数据切片采用半开区间且拒绝重叠。

这些控制降低风险，但不能抵消已列出的上线阻断项。

## 证据边界

本次只验证了静态代码、测试、Compose/脚本和当前模拟栈的只读状态。真实账户、交易所模式、资金、订单、容器内部业务新鲜度、活动参数版本、备份恢复和 live 守卫状态均为 `UNKNOWN`。任何人都不应把本摘要解释成对真实交易环境的健康证明。
