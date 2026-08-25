# 11 测试策略与覆盖审查

## 本次执行证据

| 检查 | 结果 | 解释 |
|---|---|---|
| Unit collection | 4,165 tests | 收集成功 |
| Unit execution | 4,135 passed, 30 skipped, 85 subtests | 105.06 秒，无断言失败 |
| Unit warnings | 1,665 | 主要是 Python 3.12+ sqlite datetime deprecation；另有 LongShort poller AsyncMock 协程未 await |
| Integration collection | 511 tests | 收集成功，4 个 unknown mark warning |
| Integration execution | 未执行 | 防止未经批准连接容器/外部依赖或改变状态 |
| Ruff | 9 errors | 8 unused imports + 1 unused variable，均在 tests |
| pip check | 通过 | 只证明已安装依赖元数据一致 |

首次 unit run 因 Windows 默认 pytest temp 目录权限在 87 tests 后失败；改用 audit 内 basetemp 后完整通过。这是环境问题，不是断言失败，两个结果均保留在审计说明中。

## FS-021 — 缺少仓库级 CI 质量门，绿色测试仍带大量未治理警告

- 严重度：P2；置信度：高；类别：test governance
- 状态：Phase 1/2 `VERIFIED`；Phase 3S `PARTIALLY REMEDIATED / BASE CI GATE ADDED / REMOTE ENFORCEMENT & INTEGRATION OPEN`
- 证据：仓库没有 `.github/workflows`、GitLab/Jenkins/Azure pipeline；Ruff 当前非零；pytest marker 未注册；完整 unit 虽通过但有 1,665 warnings。LongShort poller tests 的 `raise_for_status()` AsyncMock 未 await，说明 mock contract 与真实 httpx response 不一致。
- 后果：合并/发布无法自动阻止 lint、warning、schema、Compose 或风险门禁回归；warning 噪声可能掩盖真实资源/异步错误。
- 建议：建立受保护 CI：format/lint、unit、静态配置/schema check、Node UI smoke、选定 PostgreSQL/NATS/Redis integration、Compose config、secret/dependency scan；warning 按 allowlist + budget 管理，新增 warning 失败。

Phase 3S 已完成建议中的基础层：新增只读 Python 3.12 workflow，自动执行全仓 Ruff、
完整 unit、strict markers 和新增 warning 阻断；修复 Long/Short 同步 response 方法被
`AsyncMock` 建模产生的 6 条 warning。远端 run/required status check、integration、
Node/browser、Compose/schema 和 security/supply-chain gate 仍开放，详见 `39`。

Phase 3T 已把 workflow 的开放 `.[test,lint]` 解析替换为 CI hashed lock，并在安装前运行
供应链契约 verifier；这只关闭依赖漂移入口，不增加 integration/UI/schema/runtime scan
覆盖，也不证明远端 workflow 已运行。FS-022 的开放边界见 `40`。

## 覆盖强项

- unit 数量大，覆盖 settings、风险、订单、组合、账本、恢复、RDP、UI 字段语义和多种故障分支。
- integration 集合包含真实 PostgreSQL、NATS/Redis roundtrip、outbox atomicity、recovery、command flow、浏览器/E2E skeleton。
- 多处测试锁定 fee 成本/返佣符号、数据库唯一约束、CAS、stale cache 不回退、模糊恢复 fail-closed。

## 关键缺口

1. 没有 kill switch 在异步 max-size 检查期间切换的确定性竞态测试。
2. 没有 profile rollback 后 active runtime payload 真实回退的测试。
3. Backtest 测试锁定“同 bar close 成交”，但没有防 lookahead 的 next-tick invariant。
4. 没有部署脚本 trading-ready contract、live daemon required list、自动 rollback 测试。
5. Phase 1/2 没有 business task 异常退出导致 health 失败的 supervisor 测试；Phase 3D 已补 task exception/cancel/提前返回、非关键结束和 health `503`，Phase 3K 又补七条固定周期任务的 pending/stalled、checkpoint 延期、非法预算和事件驱动免误判隔离测试；事件驱动 connection/freshness/queue-lag、整体 event-loop stall 与真实依赖/容器仍缺失。
6. 没有全进程数据库连接预算/压力测试。
7. 无键盘、屏幕阅读器、reduced-motion 自动/人工回归证据。
8. 未执行本次收集到的 511 个 integration tests，当前平台下真实结果 UNKNOWN。

### Phase 3C 覆盖补充

上列第 3 项是 Phase 1/2 快照，Phase 3C 已补 10 项 FS-003 对抗测试，并更新 harness/CLI/scorecard 回归：26 focused、182 replay/backtest related 和 4,161 全量 unit 通过。它覆盖 next-event invariant、gap、unfinished/overlap、terminal expiry、no-liquidity、partial fill 与模型版本锁定。尚未完成旧 artifact lineage/策略重跑、独立 reviewer 和 L2/event execution realism，因此不能把测试覆盖等同于 G3 PASS；详见 `23`。

### Phase 3D 覆盖补充

上列第 5 项的 task-exit 部分已补：exception、unexpected cancellation、
unexpected completion 触发 nonzero；非关键 task 完成不误杀；重复 task name 拒绝；
service-owned task 可只读监督；FastAPI health `503` 不返回异常正文。41 项 focused、
178 项相关、47 项 minimal-runtime 兼容回归和最终 4,170 项全量 unit 通过。
没有完成事件驱动任务 connection/freshness/queue-lag、整体 event-loop stall、真实依赖断连、Docker restart 与告警送达，
因此不能把本轮单元测试等同于 G4 PASS；详见 `24`。

### Phase 3E 覆盖补充

上列 schema/deploy 缺口已补 16 项 FS-009 focused 测试，覆盖 Batch B ledger/idempotency/checksum/predecessor/失败停链/transaction/rollback suffix、root ledger exact match、Gateway 校验失败时零 runtime/dashboard 副作用、managed profile 禁止 DDL、deploy build/down/schema/app 顺序与 RDP URL 所有权。16 focused、220 项两组相关回归和最终 4,186 项全量 unit 通过。

新增 1 项真 PostgreSQL full-chain/idempotent/rollback-repair integration，并修正 2 组原有单 stage SQL integration 不伪造生产 ledger。共 7 项已收集，但本阶段未开启 `AATS_RUN_POSTGRES_INTEGRATION`，实际结果为 7 skipped。克隆库 manifest、锁竞争、部分失败和 app+schema rollback 仍未测，所以不能把 unit/collection 等同于 G6 PASS；详见 `25`。

### Phase 3F 覆盖补充

新增 FS-007 对抗测试，覆盖缺 profile、三个 live profile、`--yes` 不可绕过、副作用前退出、关键部署步骤不吞错、future live collector 清单、生命周期包装器模拟默认值与 legacy cleanup、模拟 evidence 的 commit/image/container 身份、显式 unknown、live/unhealthy 拒绝和排他不可覆盖写入。独立 FS-007 `11 passed`，首轮 deploy focused `34 passed`，扩大 deploy/process/startup/FS-009 相关回归为 `73 passed`，README/FS-007 文档契约 `17 passed`。最终全量 unit 为 `4197 passed, 30 skipped, 1666 warnings, 85 subtests passed in 96.41s`。

本阶段未执行 WSL2/Docker/Compose、克隆数据库、真实账户或交易所测试。因此这些测试只证明入口失败关闭和证据格式契约，不能证明完整 trading-readiness、一致 rollback 或 G5 PASS；详见 `26`。

### Phase 3G 覆盖补充

新增/更新 FS-005、`start_api.py`、Compose、deployment evidence、登录文档与进程拓扑契约测试，覆盖宿主 loopback mapping、container listener 区分、local live/non-loopback 拒绝、实际 HostIp evidence 和 malformed/all-interface 失败。扩大相关回归为 `76 passed`，最终全量 unit 为 `4219 passed, 30 skipped, 1666 warnings, 85 subtests passed in 101.93s`。

未执行 WSL2 容器重建、Docker inspect 或目标网络探测；因此单元/YAML 通过不能证明现有 HostIp、防火墙、LAN/VPN/NAT、TLS/证书或非授权网络不可达，G7 仍未放行；详见 `27`。

### Phase 3H 覆盖补充

新增 FS-020 独立 ASGI 契约测试，覆盖 HTML/JSON/HTTPException、下游弱策略覆盖、HTTP/HTTPS HSTS、本机/IPv6/畸形/恶意 Host、当前 UI 无 inline script/style/event handler 与 Gateway main 注册。初版 IPv6 归一缺陷使首轮为 `21 failed, 18 passed`，首次修正后为 `40 passed`；文档回填复核又识别并拒绝多重尾点和非 IPv6 方括号 Host，最终 focused 为 `44 passed`。全量首两次因 Windows temp/basetemp 目录基础设施各在 87 passed 后停止；中间完整重跑为 4,248 passed，Host 最终收紧后的最终全量为 `4252 passed, 30 skipped, 1666 warnings, 85 subtests passed in 95.32s`。

未执行真实 TLS terminator/proxy/browser 或未捕获 500 故障注入；因此这些测试只证明代码/ASGI 契约，不能证明目标 header 未被降级、浏览器无 CSP violation 或 G7 PASS；详见 `28`。

### Phase 3I 覆盖补充

新增 FS-019 登录异步隔离测试，覆盖 worker 不在 event loop、loop 仍可调度、capacity
超时不创建第二 worker、取消等待者不提前释放容量、global/client/identity 边界与过期、
忽略 `X-Forwarded-For`、通用 429/503、`SecretStr`/输入边界、设置失败关闭、缺失/
禁用/损坏 hash dummy KDF、iteration 上界、既有审计与 session version。

初版为 `3 failed, 61 passed`，由替身证明 QueryService 构造仍留在 event loop；移入
worker 后为 `64 passed`。最终新 auth 组合 26、配置兼容 143、扩大相关 131 项通过，
全量为 `4273 passed, 30 skipped, 1666 warnings, 85 subtests passed in 102.88s`，Ruff
通过。未执行真实 DB、多进程 limiter、trusted proxy、慢连接/挂起 worker或目标负载，
因此不能把单元测试等同于 FS-019 CLOSED 或 G7 PASS；详见 `29`。

### Phase 3J 覆盖补充

新增 FS-016 对抗测试，覆盖 generation normalize/注入拒绝、strict 条件、缺代次在 worker/Gateway 副作用前失败、generation-scoped key/payload/TTL、role/代次错配、Redis set/get 异常固定失败、peer timeout、退出精确撤回、Gateway barrier 失败不启动 publisher，以及 deploy/Compose/evidence 代次传递。

首次聚焦回归为 `113 passed`；补底层 strict generation 不变量、Gateway schema/build 前阻断和 evidence 后，扩大相关为 `129 passed, 1 warning`。最终全量为 `4286 passed, 30 skipped, 1666 warnings, 85 subtests passed in 109.12s`，Ruff、deploy shell 语法和 Compose YAML 静态解析通过。

### Phase 3K 覆盖补充

FS-006 固定周期成功进度监督新增 pending task 超时、checkpoint 延长 deadline、事件驱动任务无预算免误判、非法预算、七条任务注册契约和 stalled health 安全元数据断言。focused 为 `19 passed, 1 warning`，生命周期/health/对账/command/shadow/trial guard 扩大相关为 `118 passed, 1 warning`。最终全量为 `4296 passed, 30 skipped, 1666 warnings, 85 subtests passed in 107.00s`，application Ruff 通过。

仓库规定的原样全量命令曾因 Windows 系统临时目录权限在 87 项后中止；使用仓库内已忽略的独立 basetemp 后完整通过。未运行 integration、WSL2、容器或真实依赖故障注入，不能用本节证明目标 runtime restart/告警或事件驱动 task freshness。

未执行 Redis/NATS/Compose/WSL2 集成、并发四进程启动、单/整栈重启、断连与 INTEREST 消息计数。因此只能证明当前 Python/脚本契约，不能证明 FS-016 CLOSED 或 G5 PASS；详见 `30`。

## 推荐测试金字塔

- 每提交：Ruff 零错误、unit 零意外 warning、配置/Compose/schema 静态校验。
- 每合并：PostgreSQL事务/outbox/recovery、NATS/Redis跨进程、API contract、Node UI smoke。
- release candidate：隔离环境 4-proc compose、故障注入、迁移演练、next-tick回测回归、容量与延迟。
- live 前人工 gate：只读账户/模式/持仓/订单、reconciliation clean、kill switch 演练、备份恢复、告警送达。禁止自动提交真实订单作为测试。
### Phase 3O 覆盖补充

新增 8 项 FS-017/018 静态契约测试，覆盖原生 modal、name/description、初始/返回
焦点、Escape/backdrop、九类 trigger 传递、移除旧 aria-hidden/backdrop、
reduced-motion CSS/JS 和三处 scroll 行为；67 项 Dashboard 相关回归通过。最终全量
为 `4337 passed, 30 skipped, 1666 warnings, 85 subtests passed in 104.06s`；原样命令
仍因 Windows 系统 temp ACL 在 87 项后 setup error，仓库内唯一 basetemp 完整通过。

这只把上列第 7 项从“无自动契约”更新为“已有静态契约”；目标浏览器 keyboard-only、
NVDA/VoiceOver、axe、缩放/窄屏、高对比和 reduced-motion 人工观察仍未执行。详见
`35`，不得把静态 test 等同于辅助技术验证。

### Phase 3P 覆盖补充

新增 8 项 FS-010 配置真实性测试，覆盖四个 managed profile 零未知 Settings key、
旧伪键从 YAML/生成器/现行 reference 移除、reference 与 renderer 逐字一致、生成器不再
覆盖人工治理 README、未知 key 排序失败、非 mapping 失败、合法/空 YAML 兼容，以及
`load_settings()` 不能旁路 loader。focused 为 `8 passed, 1 warning`；env/settings/FS-010
组合为 `56 passed, 1 warning, 2 subtests passed`。

仓库规定的原样全量命令在 `87 passed` 后因 Windows 系统 temp ACL setup error 中止；
仓库内全新 basetemp 完整结果为
`4345 passed, 30 skipped, 1666 warnings, 85 subtests passed in 95.17s`，Ruff 通过。
这些结果证明代码/隔离配置契约，不证明 committed candidate 的目标进程启动、仓库外
overlay 或真实运行状态；详见 `36`。

### Phase 3Q 覆盖补充

新增 6 项 FS-011 测试，覆盖旧入口无 runtime/profile imports、无参数/旧参数固定
stderr+exit 2、live 参数拒绝、真 subprocess 与 decision main 同步无参签名。focused
为 `6 passed`，隔离 basetemp related 为 `51 passed`，全量为
`4351 passed, 30 skipped, 1666 warnings, 85 subtests passed in 108.36s`。原样全量仍在
87 项后被 Windows temp ACL 阻断。未做仓库外调用方盘点或 committed candidate 独立
复核；详见 `37`。

### Phase 3R 覆盖补充

新增 17 项 FS-015 对抗测试，覆盖 gate 默认/false 序列化、CLI JSON boolean、非布尔
direct/from-dict 失败关闭、关闭时跳过 short 计算并在 history 前钳制、bearish vector
开启/关闭分叉、生产/replay disabled golden contract，以及该全局能力开关不能进入
按 combo 发布的 active-parameter mapping。focused 为 `17 passed`，扩大 replay/scoring/
CLI/active-parameter 组合为 `132 passed`；仓库内全新 basetemp 全量为
`4368 passed, 30 skipped, 1666 warnings, 85 subtests passed in 108.26s`，Ruff 通过。

原样全量仍在 87 项后被 Windows 系统 temp ACL 阻断。没有运行 WSL2 integration、历史
artifact 重跑或 independent committed-candidate review；详见 `38`。

### Phase 3S 覆盖补充

新增 7 项 FS-021 workflow/依赖/marker/mock 契约测试；与 Long/Short 相关测试合并为
`17 passed`，同一 focused 在 `-W error` 与精确 SQLite datetime allowlist 下为
`17 passed`、0 warning。全仓 Ruff 通过，unit strict-marker collection 为
`4405 tests collected`，标准全量为
`4375 passed, 30 skipped, 1660 warnings, 85 subtests passed in 107.96s`。

本机 `.venv` 是 Python 3.14，而 workflow/生产镜像目标为 3.12；本机全量 `-W error`
在 339 项处暴露 3.14 SQLite connection finalizer `ResourceWarning`，因此不冒充远端
3.12 CI 已通过。剩余 1,660 条标准 warning 全部是 SQLite datetime adapter 弃用类，
workflow 只精确 allowlist 该消息；远端执行、required check、integration/security gate
与 allowlist 清零仍未完成。详见 `39`。

### Phase 3T 覆盖补充

新增 6 项 FS-022 锁文件、Docker、workflow 与 Compose image digest 契约；与更新后的
FS-021 contract 合并为 `13 passed`。标准库 verifier 输出
`runtime=46 ci=33 external_images=9`；两份 lock 的 46/33 个 Linux CPython 3.12 wheel
均完成逐条 SHA-256 下载验证。没有 Docker CLI，未执行 clean build、远端 CI、SBOM 或
扫描。strict marker collection 为 `4411 tests collected`，仓库内唯一 basetemp 的完整
unit 为 `4381 passed, 30 skipped, 1659 warnings, 85 subtests passed in 108.44s`，全仓
Ruff 通过；因此仍不扩大为供应链 CLOSED 证据，详见 `40`。

### Phase 3U 覆盖补充

新增 12 项 FS-008 预算/角色/engine inventory 对抗测试，其中三个样例证明 SQLAlchemy
同步 factory 别名、模块别名和 async factory 别名不能绕过扫描。与 database runtime、
orderbook、managed config generator/env profile 的 focused 组合为 `56 passed`；标准库
verifier 输出 `declared_ceiling=150 operational_reserve=47 components=14 engine_calls=13`。

strict marker collection 为 `4423 tests collected`；原样完整 unit 仍在 87 项后被 Windows
系统 temp ACL 阻断，仓库内唯一 basetemp 重跑为
`4393 passed, 30 skipped, 1659 warnings, 85 subtests passed in 115.81s`。应用 Ruff `--fix`
和全仓 Ruff 均通过。未运行 WSL2/PostgreSQL integration、目标容量、慢查询/故障重连、
恢复/admin 竞争、联合内存或告警验证，故 FS-008 仍是部分整改，详见 `41`。

### Phase 3V 覆盖补充

FS-004 + real-data/recommendation/gold focused 为 `57 passed`；其中包含 test timestamps
不进入 factor evaluator、train fail/valid pass 不生成 candidate、holdout-only 内容变化
保持 development metrics 不变但改变 seal、development evidence lineage 闭环与 v2
recommendation 失败关闭，以及全窗口/test execution summary 被拒绝。Research Factory
与 Phase 4 summary identity helper 扩大回归为 `327 passed`；focused Ruff 通过。

strict marker collection 为 `4439 tests collected`；原样完整 unit 在 `87 passed` 后被
Windows 系统 Temp ACL 阻断，仓库内唯一 basetemp 完整复跑为
`4409 passed, 30 skipped, 1660 warnings, 85 subtests passed in 115.95s`。应用/全仓 Ruff、
dependency/connection verifiers、pip check、16 份 YAML、789 份 Markdown 的 1,049 个本地
目标和 diff check 均通过。

没有读取历史 artifact/registry 或运行最终 OOS/walk-forward；单元测试不能证明历史 test
未污染或新 holdout 只会被访问一次，详见 `42`。
