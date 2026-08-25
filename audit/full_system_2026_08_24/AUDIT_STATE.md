# 审计状态

> 状态：Phase 2 P1 对抗复核完成；Phase 3A–3W 代码/隔离回归已追加，但运行、功能、目标环境、远端 CI/完整供应链、外部配置/调用方、历史证据重跑、最终 OOS 与独立复核门禁仍未关闭，不得作为上线批准  
> 冻结时间：2026-08-24T05:54:05.9618652-04:00  
> 最后核对：2026-08-25  
> 基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`（`main`，本地领先 `origin/main` 1 个提交）  
> 基线提交时间：2026-08-24T00:06:54-04:00  
> 基线提交标题：`fix: enforce execution-owned recovery snapshots`

## 冻结证据

- 仓库根：`D:/文件/project/AIParticipatingAutonomousTradingSystem`
- 冻结时工作区：tracked/staged diff 均为空，未跟踪文件 0。
- tracked diff 指纹：`e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`。
- 当前预期工作区差异仅为 `audit/`；若出现其他差异，视为外部漂移。
- WSL2 `~/aats` 在本次检查时与 Windows HEAD 一致。

## 工具与运行环境

| 项目 | 本次值 |
|---|---|
| Git | 2.55.0.windows.4 |
| PowerShell | 7.6.4 |
| Windows | 10.0.26200 |
| Python | 3.14.0（项目 `.venv`） |
| pytest | 9.0.2 |
| Ruff | 0.15.8 |
| Node.js | v25.2.1 |
| WSL | 2.5.10.0，kernel 6.6.87.2-1 |
| Docker Server（WSL） | 29.1.3 |
| Windows Docker CLI | 未安装/不可用 |

## 仓库清单

- tracked 文件：2,019。
- 主要目录：`docs` 730、`aats` 596、`tests` 419、`scripts` 127、`configs` 70、`deploy` 37、`apps` 9、`migrations` 5。
- 主要行数：`aats` 227,687、`tests` 189,119、`docs` 113,524、`scripts` 46,123、`deploy` 6,575、`configs` 4,555。
- 代码核心：`aats/services` 约 102,553 行；`aats/data_platform` 约 58,779 行；`aats/storage` 约 13,347 行。
- 运行时导入得到 193 条 FastAPI 路由；静态 ORM 元数据包含 49 张交易库表、78 张 RDP 表。

## 已完成的证据工作

- 完整读取现行仓库约束、入口文档、Compose/部署入口、主进程入口和选定关键模块。
- 从配置合成追踪到 runtime composition、NATS、行情/特征/决策、风控/执行、成交/组合/账本/恢复。
- 对资金关键链路完成第二遍调用方与测试交叉核对。
- 对研究工厂、回测成交时序、数据库建表/迁移入口、API 权限、UI 关键交互与部署健康检查做专题审查。
- 建立逐 tracked 文件覆盖矩阵；未读文件明确标为 `DISCOVERED BUT NOT YET REVIEWED`。

## 安全运行验证

- 当前 WSL 运行的是模拟衍生品 overlay，不是 live overlay。
- 主容器和基础设施容器显示运行约 6 小时且为 healthy；这只证明容器级存活。
- Gateway 实际绑定 `HostIp=""`、宿主端口 8001；`http://127.0.0.1:8001/login` 返回 200。
- 当前 PostgreSQL `max_connections=200`，只读采样时 `pg_stat_activity=40`。
- 当前模拟衍生品库有 49 张 public 表；RDP 库七个 schema 共 78 张表。
- 未查询账户余额、持仓、订单、成交、风控开关或收益。

## 验证结果

- 单元测试：`4,135 passed, 30 skipped, 1,665 warnings, 85 subtests passed`，105.06 秒。
- 集成测试仅收集：511 项，收集成功；未执行，避免未经批准连接外部依赖或改变状态。
- Ruff：9 项错误，均在测试文件（8 个未使用 import、1 个未使用变量）。
- `pip check`：无破损依赖关系。
- 依赖漏洞扫描：UNKNOWN；环境无 `pip-audit`/`safety`，且审计约束禁止修改依赖。

## 尚未完成

- 2,019 个 tracked 文件并未逐行全部审完；状态见 CSV，严禁声称全覆盖。
- 未做真实交易所、真实账户、live profile、故障注入、灾备恢复演练和浏览器辅助技术实测。
- 未执行 511 个集成测试、性能压测、依赖 CVE 联网扫描、迁移前滚/回滚演练。
- 未验证宿主防火墙、局域网拓扑、证书信任、备份可恢复性、告警送达链路。

## 续审起点

Phase 2 已逐项反证首轮九个 P1：`FS-002` 升为 P0；`FS-001/003/006/007/009` 保持 P1；`FS-004/005/008` 降为 P2 但仍需历史或目标环境验证。证据见 `17` 至 `20`。

下一步首先关闭 `FS-002` 的 halt acknowledgement/fencing 硬门禁。修复前不要进入 live 上线流程。任何修复均应在新任务中先设计、经人工批准、实现、做隔离测试，再执行受控部署；本审计未实施任何修复。

## Phase 3A 追加状态

Phase 2 “本审计未实施修复”是历史冻结陈述。随后的 Phase 3A 已在 `codex/fs-002-kill-switch-p0` 上实施未提交的 FS-002 修复，完成 12 项定向回归、相关回归和 4,147 项全量单测（最终运行 122.55 秒）。真实 Redis/NATS/四进程故障注入因环境/可选依赖不具备而未执行，所以 FS-002 状态为 `PARTIALLY REMEDIATED / OPEN`。权威证据见 [21-fs-002-remediation.md](21-fs-002-remediation.md)；生产决定仍为 **NO-GO**。

## Phase 3B 追加状态

Phase 3B 已在同一未提交工作区将 FS-001 profile rollback 端点改为授权后无写入 `501`，修复前的 `ok=true/status=rolled_back` 虚假成功与 recommendation 终态写入已被阻止。新增 4 项定向测试，50 项相关回归与最终 4,151 项全量单测通过（最终运行 103.72 秒）。真 reverse saga、research/live/runtime 同 generation 读回与隔离双库故障注入尚未实现，所以 FS-001 状态为 `PARTIALLY REMEDIATED / OPEN`。权威证据见 [22-fs-001-profile-rollback-fail-closed.md](22-fs-001-profile-rollback-fail-closed.md)；生产决定仍为 **NO-GO**。

## Phase 3C 追加状态

Phase 3C 已在同一未提交工作区把 backtest 固定为 `next_bar_event_v2`：完整 K 线只在 bar end 决策/提交，IOC/bounded 最早按下一可用 bar open 成交，post-only 在下一完整 bar close 后解析；未闭合/重复/倒序/重叠数据失败关闭，terminal order 过期，no-fill/partial fill 与 adapter 状态按实际成交同步。新增 10 项对抗测试，26 项 focused、182 项 replay/backtest 相关回归和最终 4,161 项全量单测通过（94.69 秒）。旧 artifact 清单/重跑、独立复核和真实盘口现实性尚未完成，所以 FS-003 状态为 `CODE REMEDIATED / REVIEW & EVIDENCE RE-RUN OPEN`，G3 仍未放行。权威证据见 [23-fs-003-backtest-causal-timing-remediation.md](23-fs-003-backtest-causal-timing-remediation.md)；生产决定仍为 **NO-GO**。

## Phase 3D 追加状态

Phase 3D 已在同一未提交工作区为关键长期 asyncio task 增加显式注册与结束监督：daemon 在 task exception、unexpected cancellation 或 unexpected completion 时停止独立 heartbeat、清理并返回 `1`；FastAPI `/healthz` 在已观测关键 task 失败时返回 `503`。修复前的“task dead + heartbeat alive + external stop 后 exit 0”隔离复现已翻转为无需外部 stop 的 `exit 1`。41 项 focused、178 项相关回归、47 项兼容回归和最终 4,170 项全量单测通过（102.85 秒）。永久 hang、last-success/lag、依赖断连、真实四进程 Docker restart/告警和独立复核尚未完成，所以 FS-006 状态为 `PARTIALLY REMEDIATED / HANG-LAG RUNTIME VERIFICATION OPEN`，G4 仍未放行。权威证据见 [24-fs-006-critical-task-supervision-remediation.md](24-fs-006-critical-task-supervision-remediation.md)；生产决定仍为 **NO-GO**。

## Phase 3E 追加状态

Phase 3E 已在同一未提交工作区将主交易 root migration、RDP ORM baseline 和全部 13 个 Batch B stage 收口到部署期一次性 schema job；RDP 新增 version/checksum ledger、advisory lock、前置与 suffix rollback contract，DDL 与 ledger 在同一 transaction 提交。四个 managed profile 禁止 runtime DDL，Gateway 在任何 readiness/后台 task 前只读校验，失败不 ready。16 项 focused、220 项两组相关回归、最终 4,186 项全量单测通过（85.02 秒）。新增的 PostgreSQL integration 及两个相关 SQL integration 共 7 项已收集，因未开启隔离 DB 环境门而全部跳过；未连接任何数据库。空库/历史克隆/部分失败 manifest 全等与 app+schema rollback 尚未演练，所以 FS-009 状态为 `PARTIALLY REMEDIATED / CLONE MANIFEST & ROLLBACK OPEN`，G6 仍未放行。权威证据见 [25-fs-009-schema-single-truth-remediation.md](25-fs-009-schema-single-truth-remediation.md)；生产决定仍为 **NO-GO**。

## Phase 3F 追加状态

Phase 3F 已在同一未提交工作区将标准部署入口改为无默认 profile，只允许显式 `spot`/`derivatives` 模拟 profile；三个 live profile 在任何 WSL/Docker/DB 副作用前硬失败，且无 `--yes` override。down/infra/app up 非零不再被吞，future live required list 补齐两个 collector，并新增明确 `production_ready=false`/`trading_ready=false` 的不可覆盖模拟部署 evidence。生命周期包装器默认模拟并阻止 live 启动，同时保留遗留 live 的 Stop/Status/Remove 清理路径。11 项独立 FS-007、34 项 deploy focused、73 项扩大相关与 17 项文档契约回归通过；最终全量为 `4197 passed, 30 skipped, 1666 warnings, 85 subtests passed in 96.41s`，Ruff、shell/PowerShell syntax、lifecycle dry-run、60 个变更 Markdown 链接和 diff check 均通过。未执行 WSL2/Docker/数据库/交易所。完整 trading-readiness packet、克隆环境部署/一致回滚和独立复核仍未实现，所以 FS-007 状态为 `RISK CONTAINED / LIVE DISABLED / READINESS & CONSISTENT ROLLBACK OPEN`，G5 仍未放行。权威证据见 [26-fs-007-deployment-fail-closed-remediation.md](26-fs-007-deployment-fail-closed-remediation.md)；生产决定仍为 **NO-GO**。

## Phase 3G 追加状态

Phase 3G 已在同一未提交工作区把 Gateway Compose 宿主 mapping 固定到 `127.0.0.1`，保持 container 内 `0.0.0.0` 仅供 Docker network；本地 `start_api.py` 只允许模拟 profile 与 loopback host。模拟 deployment evidence 现在读取并记录实际 Docker published binding，缺失、格式错误、空/all-interface 或非 loopback HostIp 都失败。76 项 FS-005/start_api/FS-007/deploy/login/process related 通过；最终全量为 `4219 passed, 30 skipped, 1666 warnings, 85 subtests passed in 101.93s`。没有启动或重建容器，也没有网络探测。FS-005 当前状态为 `CODE REMEDIATED / TARGET NETWORK VERIFICATION OPEN`；实际 HostIp、防火墙、LAN/VPN/NAT、TLS/证书、Host/auth/cookie/限流仍 UNKNOWN，G7 未放行。权威证据见 [27-fs-005-gateway-loopback-containment.md](27-fs-005-gateway-loopback-containment.md)；生产决定仍为 **NO-GO**。

## Phase 3H 追加状态

Phase 3H 已在同一未提交工作区为 Gateway 新增最外层 user middleware：Host 仅允许 `127.0.0.1`/`localhost`/`::1`/`testserver`，畸形或不信任值在路由前返回 400；HTML、JSON、HTTPException/认证失败和 Host 400 响应统一覆盖严格 CSP、frame/nosniff/referrer/permissions/COOP/CORP 头，HSTS 只在实际 HTTPS scope 输出。首轮因内部 IPv6 loopback 规范化缺陷为 21 failed/18 passed，首次修正后 40 focused 通过；后续复核又拒绝多重尾点和非 IPv6 方括号 Host，最终为 `44 focused passed`与 `4252 passed, 30 skipped, 1666 warnings, 85 subtests passed in 95.32s`。全量早期两次还曾因 Windows temp/basetemp 目录问题各在 87 passed 后停止，已在已忽略的独立目录完整重跑。未执行真实 TLS/proxy/browser、容器或网络验证，框架最外层未捕获 500 响应头仍待故障注入。FS-020 当前状态为 `CODE & ASGI REMEDIATED / TARGET TLS-BROWSER VERIFICATION OPEN`，G7 仍未放行。权威证据见 [28-fs-020-browser-security-headers-remediation.md](28-fs-020-browser-security-headers-remediation.md)；生产决定仍为 **NO-GO**。

## Phase 3I 追加状态

Phase 3I 已在同一未提交工作区把 `POST /auth/login` 的同步 Operator repository、390,000 轮 PBKDF2、账户状态和审计写入完整移入有界 `asyncio.to_thread` worker；每 FastAPI app/loop 默认最大并发 4、排队 1 秒，取消等待者不会在真实 worker 完成前释放 capacity。新增每进程 60 秒 global/client/identity 60/20/10 滑动窗口，不信任 `X-Forwarded-For`；不存在/禁用/损坏 hash 走 dummy KDF，hash iteration 有 1,000,000 上界，username/password 有 128/1024 输入边界和 `SecretStr`。初版 focused 为 `3 failed, 61 passed`，暴露 QueryService 构造仍在 event loop，移入 worker 后 `64 passed`；最终 auth 组合 26、配置兼容 143、扩大相关 131 项通过，全量为 `4273 passed, 30 skipped, 1666 warnings, 85 subtests passed in 102.88s`，Ruff 通过。未执行真实 DB、多进程集中限流、trusted proxy、慢连接/挂起 worker、目标容量或告警验证，所以 FS-019 为 `CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN`，G7 仍未放行。权威证据见 [29-fs-019-operator-login-async-isolation.md](29-fs-019-operator-login-async-isolation.md)；生产决定仍为 **NO-GO**。

## Phase 3J 追加状态

Phase 3J 已在同一未提交工作区将 gateway/market/decision/execution 四主进程的 NATS/hybrid peer readiness 收紧为失败关闭：缺部署 generation/hot-state、Redis announce/poll 异常或 peer timeout 均在 publisher 前失败。标准模拟 deploy 在 sync 后/build 前生成非秘密 generation，Compose required interpolation 注入；ready key/payload 和不可覆盖模拟 evidence 都绑定该代次，旧 key 不能满足新 deploy。首轮聚焦回归 `113 passed`；加固底层 generation 不变量和 evidence 后，扩大相关为 `129 passed, 1 warning`，全量为 `4286 passed, 30 skipped, 1666 warnings, 85 subtests passed in 109.12s`；Ruff、deploy shell 语法与 Compose YAML 静态解析通过。未执行 WSL2/Docker/Redis/NATS/DB/交易所或目标启动、重启、断连矩阵，所以 FS-016 为 `CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN`，G5 仍未放行。权威证据见 [30-fs-016-nats-peer-readiness-remediation.md](30-fs-016-nats-peer-readiness-remediation.md)；生产决定仍为 **NO-GO**。

## Phase 3K 追加状态

Phase 3K 在 Phase 3D task-exit supervisor 之上，为账户刷新、执行同步、对账、execution outbox、execution command flow、Phase 1 shadow 和 trial guard 七条固定周期关键循环增加进程内成功进度 deadline。永久 pending await 或连续无成功周期达到 `max(60s, 3 × interval)` 后分类为 `stalled`，daemon 复用 heartbeat stop/cleanup/exit `1`，FastAPI `/healthz` 返回 `503`；checkpoint 使用单调时钟，事件驱动任务不套用统一静默阈值。19 项 focused、118 项扩大相关和最终 `4296 passed, 30 skipped, 1666 warnings, 85 subtests passed in 107.00s`，Ruff 通过。原样全量命令曾因 Windows 系统临时目录权限在 87 项后中止，改用仓库内隔离 basetemp 后完整通过。未执行 WSL2/Docker/真实依赖/integration；事件驱动任务、整体 event-loop stall、真实 restart/告警和独立复核仍未完成，所以 FS-006 为 `PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN`，G4 仍未放行。权威证据见 [31-fs-006-critical-task-progress-watchdog.md](31-fs-006-critical-task-progress-watchdog.md)；生产决定仍为 **NO-GO**。

## Phase 3L 追加状态

Phase 3L 在 Phase 3A generation/ack/final submission fence 上增加控制面短时交易许可：Gateway/monolith 仅在长期 authority 为同 generation RUNNING 时维护 15 秒 Redis permission（每 5 秒续租），execution 最终 fence 必须同时读取长期 authority 和同 generation permission，且不能自行续租。四进程代理 resume 还必须由 Gateway 重读同 generation authority 并成功激活 permission 后才返回 Operator 成功。halt/shutdown 尽力删除前一 RUNNING key；Redis/NATS halt 传播与 permission 删除同时失败时，纯内存故障注入证明旧 permission 仍按 TTL 到期。lease task 作为 service-owned critical task 监督。28 项 FS-002 focused、225 项扩大相关与最终 `4312 passed, 30 skipped, 1666 warnings, 85 subtests passed in 103.70s`；原样全量命令仍因 Windows 系统临时目录权限在 87 项后中止，仓库内全新 basetemp 完整通过。未执行真实 Redis/NATS/四进程、单向分区、kill -9、目标 restart/告警、真实 OKX 或独立复核，所以 FS-002 为 `PARTIALLY REMEDIATED / TARGET PARTITION-EXPIRY VERIFICATION OPEN`，G1 仍未放行。权威证据见 [32-fs-002-short-lived-trading-permission-lease.md](32-fs-002-short-lived-trading-permission-lease.md)；生产决定仍为 **NO-GO**。

## Phase 3M 追加状态

Phase 3M 证明 profile apply 的历史四步 Saga 不能构成 runtime 生效证据：live 查询使用不存在于现行 activation model 的 `profile_id`，写入的 threshold 不属于 `StrategyProfileActivationState`，profile active-set 行也没有按目标 worker 加载/回读的 generation 协议。`POST /rdp/profile-recommendations/{id}/apply` 现与 Phase 3B rollback 一样，在 token/actor、`released` 状态和双人签署检查后稳定返回无写入 `501`；不再检查/打开 live pool、创建/续跑 Saga、写 research/live 数据或标记 `applied`。17 项 FS-001 direct+historical-Saga、`321 passed, 1 skipped, 100 warnings, 6 subtests` 的 RDP/profile 扩大相关，以及最终 `4317 passed, 30 skipped, 1665 warnings, 85 subtests passed in 115.56s` 全量 unit 通过。原样全量命令仍因 Windows 系统临时目录权限在 87 项后 setup error，仓库内全新 basetemp 完整通过。未执行真实 DB、worker、runtime readback、历史 applied 数据对账或独立复核，所以 FS-001 为 `PARTIALLY REMEDIATED / PROFILE APPLY & ROLLBACK FAIL-CLOSED / RUNTIME ACTIVATION OPEN`，G2 仍未放行。权威证据见 [33-fs-001-profile-apply-fail-closed.md](33-fs-001-profile-apply-fail-closed.md)；生产决定仍为 **NO-GO**。

## Phase 3N 追加状态

Phase 3N 将离线 fill 固定为 `ohlcv_participation_cap_v2`：IOC、post-only 与
bounded-limit 都要求正 volume 并受默认 1% participation cap，超量产生 partial
fill；IOC/bounded next-open 只使用下单前已闭合 observation volume，bounded 按
taker fee + fixed slippage。CostDiagnostic/scorecard 分开保存实际 fee/slippage，
meta 明示 OHLCV 与 L2/queue/spread/impact/latency 限制。106 focused、119 related，
最终 `4329 passed, 30 skipped, 1666 warnings, 85 subtests passed in 98.83s`；原样
全量命令仍因 Windows 系统临时目录权限在 87 项后 setup error，仓库内全新
basetemp 完整通过。Ruff 通过。未执行真实 L2/trades/fill 校准、旧 artifact/策略重跑、
容量置信区间或独立复核，所以 FS-014 为 `PARTIALLY REMEDIATED / OHLCV CONTAINED /
L2 CALIBRATION OPEN`，G3 仍未放行。权威证据见
[34-fs-014-ohlcv-fill-realism-containment.md](34-fs-014-ohlcv-fill-realism-containment.md)；
生产决定仍为 **NO-GO**。

## Phase 3O 追加状态

Phase 3O 将 Dashboard 详情抽屉由 `<aside aria-hidden>` + 独立 backdrop 改为原生
modal `<dialog>`，补 accessible name/description、异步 trigger 传递、初始/返回焦点、
Escape/backdrop/按钮统一关闭；`prefers-reduced-motion: reduce` 停止 CSS animation、
transition、smooth scroll、已知 hover 位移和 JavaScript 三处显式 smooth scroll。
8 项新增、67 项 Dashboard 相关通过；原样全量在 87 项后因 Windows `tmp_path`
PermissionError 中止，仓库内全新 basetemp 为
`4337 passed, 30 skipped, 1666 warnings, 85 subtests passed in 104.06s`。Ruff 与三份
JavaScript Node 语法检查通过。未启动 Dashboard，未运行 WSL2 integration、目标
浏览器、keyboard-only、NVDA/VoiceOver、axe、缩放或 reduced-motion 视觉观察，故
FS-017 为 `CODE REMEDIATED / TARGET BROWSER & ASSISTIVE-TECH VERIFICATION OPEN`，
FS-018 为 `CODE REMEDIATED / TARGET REDUCED-MOTION VERIFICATION OPEN`。权威证据见
[35-fs-017-fs-018-dashboard-accessibility.md](35-fs-017-fs-018-dashboard-accessibility.md)；
生产决定仍为 **NO-GO**。

## Phase 3P 追加状态

Phase 3P 删除了四个 managed strategy YAML、生成器和现行字段参考中没有 Settings 字段
或行为消费者的 `strategy_profile_auto_rollback_enabled`；没有用空壳字段伪装统一自动
回滚能力。managed loader 现在要求 YAML 为 mapping，并对代码 runtime defaults 与 YAML
全部 key 使用 `AATSSettings.model_fields` 失败关闭校验。生成器 reference 与现行文档
逐字一致，且不再覆盖人工治理的 `configs/README.md`。8 项 focused、56 项相关通过；
原样全量在 87 项后因 Windows `tmp_path` PermissionError 中止，仓库内全新 basetemp 为
`4345 passed, 30 skipped, 1666 warnings, 85 subtests passed in 95.17s`，Ruff 通过。
未执行 WSL2/integration、目标 profile 启动、仓库外 overlay 盘点、generator clean-run 或
独立复核，所以 FS-010 为 `CODE REMEDIATED / MANAGED UNKNOWN-KEY FAIL-CLOSED /
TARGET STARTUP VERIFICATION OPEN`。权威证据见
[36-fs-010-managed-profile-unknown-key-fail-closed.md](36-fs-010-managed-profile-unknown-key-fail-closed.md)；
生产决定仍为 **NO-GO**。

## Phase 3Q 追加状态

Phase 3Q 将失效的 `scripts/run_local.py` 收口为无配置副作用的迁移失败入口：保留旧参数
识别，但不加载 dotenv、不导入 runtime，固定 stderr 指引与 exit `2`。6 项 focused、
51 项相关通过；原样全量在 87 项后因 Windows `tmp_path` PermissionError 中止，仓库内
全新 basetemp 为 `4351 passed, 30 skipped, 1666 warnings, 85 subtests passed in
108.36s`，Ruff 通过。committed candidate 独立复核与仓库外调用方迁移仍 OPEN。
权威证据见 [37-fs-011-legacy-run-local-fail-closed.md](37-fs-011-legacy-run-local-fail-closed.md)；
生产决定仍为 **NO-GO**。

## Phase 3R 追加状态

Phase 3R 为 independent replay 增加生产同名 strict boolean
`strategy_short_bias_enabled`：值进入 CLI/from-dict/to-dict 与实验参数证据；关闭时在
score history、dominant-leg 和状态机前跳过 short 计算并固定 short score `0.0`。
该全局能力开关明确保持 replay-only，不进入按 family/timeframe 分片的 active-parameter
映射。17 项 focused、132 项相关通过；原样全量仍在 87 项后因 Windows `tmp_path`
PermissionError 中止，仓库内全新 basetemp 为
`4368 passed, 30 skipped, 1666 warnings, 85 subtests passed in 108.26s`，Ruff 通过。
未执行 WSL2/integration、历史 artifact 重跑、目标运行或独立复核，所以 FS-015 为
`CODE REMEDIATED / HISTORICAL EVIDENCE RE-RUN & INDEPENDENT REVIEW OPEN`。权威证据见
[38-fs-015-replay-short-bias-parity.md](38-fs-015-replay-short-bias-parity.md)；生产决定仍为
**NO-GO**。

## Phase 3S 追加状态

Phase 3S 新增只读 Python 3.12 GitHub Actions 基础门禁，执行全仓 Ruff、完整 unit、
strict markers 和新增 warning 阻断；Actions 固定完整 SHA，checkout 不保留凭据，且
workflow 不读取 secrets、不运行 Docker/部署。Long/Short 测试的同步
`raise_for_status()` 已由 `AsyncMock` 改为 `Mock`，标准 warning 从 1,666 降为 1,660，
剩余均为同一 SQLite datetime adapter 弃用类。7 项新增/17 项 focused 通过，unit strict
marker collection 为 4,405，标准全量为
`4375 passed, 30 skipped, 1660 warnings, 85 subtests passed in 107.96s`，全仓 Ruff 通过。

本机 Python 3.14 全量 `-W error` 另暴露 SQLite connection finalizer
`ResourceWarning`，不能冒充目标 Python 3.12 workflow 实跑；GitHub 远端 run/required
check、integration/UI/Compose/schema/security gate、allowlist 清零与 FS-022 仍开放。
FS-021 为 `PARTIALLY REMEDIATED / BASE CI GATE ADDED / REMOTE ENFORCEMENT & INTEGRATION OPEN`。
权威证据见 [39-fs-021-ci-quality-gate.md](39-fs-021-ci-quality-gate.md)；生产决定仍为
**NO-GO**。

## Phase 3T 追加状态

Phase 3T 为 CPython 3.12/Linux x86_64 新增 runtime/CI 完整版本与 SHA-256 lock；Docker
和 CI 均按 hash/binary 安装，项目源码安装禁止再次解析依赖。Python 两个 stage 与九个
外部 Compose image 固定 manifest digest；标准库 verifier 和 6 项 FS-022 contract tests
防止恢复开放解析或 tag-only 引用。runtime 46 个、CI 33 个目标 wheel 均完成逐条 hash
下载验证；FS-021/022 合并 focused 为 `13 passed`，focused Ruff 通过。

strict marker collection 为 `4411 tests collected`；原样完整 unit 仍在 87 项后被 Windows
系统 temp ACL 阻断，仓库内唯一 basetemp 重跑为
`4381 passed, 30 skipped, 1659 warnings, 85 subtests passed in 108.44s`。全仓 Ruff、
pip check、8 份 workflow/Compose YAML 静态解析、785 份 Markdown 的 1,230 个本地链接
和 diff check 均通过。

当前主机没有 Docker CLI，未执行 clean image build；远端 Python 3.12 workflow、APT
snapshot、SBOM、CVE/license/secret/signature/provenance、integration 和独立复核均未完成。
FS-022 为
`PARTIALLY REMEDIATED / PYTHON LOCKS & IMAGE DIGESTS ADDED / APT-SBOM-CVE & CLEAN BUILD OPEN`。
权威证据见
[40-fs-022-reproducible-dependencies.md](40-fs-022-reproducible-dependencies.md)；生产决定仍为
**NO-GO**。

## Phase 3U 追加状态

Phase 3U 为 FS-008 新增数据库连接预算单一真源：四个主进程按角色使用 32/8/10/16 的
ceiling，当前 14 个声明 topology component 合计 150；Compose 普通容量 197、名义余量
47。13 个应用 `create_engine` 调用全部进入 AST inventory，SQLAlchemy sync/async factory
别名也不能绕过；短命 CLI/replay 使用 `NullPool`。Compose 容量和 CI verifier 接入均有
防回退契约。

FS-008 contract 为 `12 passed`，扩大 focused 为 `56 passed`；strict marker collection
为 `4423 tests collected`。原样完整 unit 在 87 项后仍被 Windows temp ACL 阻断，仓库内
唯一 basetemp 重跑为
`4393 passed, 30 skipped, 1659 warnings, 85 subtests passed in 115.81s`。应用与全仓 Ruff、
dependency/connection verifiers、pip check、8 份 YAML、787 份 Markdown 的 1,031 个相对
本地目标和 diff check 均通过。

未运行 WSL2/PostgreSQL integration、目标全拓扑负载、慢查询/故障重连、transient/CLI/
迁移/恢复/admin 叠加、联合内存和告警验证。FS-008 为
`PARTIALLY REMEDIATED / DECLARED TOPOLOGY BUDGETED / TARGET LOAD & TRANSIENT PATHS OPEN`。
权威证据见
[41-fs-008-database-connection-budget.md](41-fs-008-database-connection-budget.md)；生产决定仍为
**NO-GO**。

## Phase 3V 追加状态

Phase 3V 为 FS-004 将 real-data runner 升级为
`train_valid_selection_test_holdout_v2`：train 与 valid 独立评估并要求双门通过，valid
作为 candidate metrics benchmark；test 只参与输入质量/来源一致性 gate，不进入 factor、
label、绩效 metrics 或 selection gate；它写入精确内容 seal、`sealed_not_evaluated` 和
`metrics_exposed=false`。candidate、recommendation、
manifest 与 development evidence ref 形成失败关闭的 lineage；新 recommendation 明示当前
metrics 只是 development evidence。

FS-004 focused 为 `57 passed`，Research Factory + Phase 4 contract 扩大回归为
`327 passed`，focused Ruff 通过。strict marker collection 为 `4439 tests collected`；原样
完整 unit 在 `87 passed` 后被 Windows 系统 Temp ACL 阻断，仓库内唯一 basetemp 完整复跑为
`4409 passed, 30 skipped, 1660 warnings, 85 subtests passed in 115.95s`。应用/全仓 Ruff、
dependency/connection verifiers、pip check、16 份 YAML、789 份 Markdown 的 1,049 个本地
目标和 diff check 均通过。历史 artifact/registry、真实 Gold 数据和人工查看记录未读取，
最终 OOS、一次性 access ledger、walk-forward、多重检验和独立复核均未完成。

FS-004 当前为
`PARTIALLY REMEDIATED / TEST SEALED FROM CANDIDATE SELECTION / FINAL OOS & HISTORY AUDIT OPEN`。
权威证据见
[42-fs-004-research-selection-holdout.md](42-fs-004-research-selection-holdout.md)；生产决定仍为
**NO-GO**。

## Phase 3W 追加状态

Phase 3W 对起始基线以来的全部整改候选重新复审，并补齐登录/Kill Switch/回测成交的非有限
数值边界、本地 monolith 入口、CI warning allowlist、SQLite test engine 释放和 Compose
内嵌运维说明。全仓 Ruff、依赖锁、数据库连接预算、managed config generator 与 diff check
通过；严格完整 unit 为 `4423 passed, 30 skipped, 94 subtests passed`。

上述结果只关闭本轮静态/Windows 隔离范围内的新确认缺陷。WSL2 Postgres integration、标准
derivatives 模拟部署、NATS/Redis 四进程、系统恢复、交易所只读状态与持续日志仍待现场验证。
权威证据见
[43-phase3w-post-audit-full-change-review.md](43-phase3w-post-audit-full-change-review.md)；生产决定仍为
**NO-GO**。
