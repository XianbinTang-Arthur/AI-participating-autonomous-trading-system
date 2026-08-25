# FS-007 部署失败关闭、证据包与实盘禁用范围

> 文档状态：Phase 3F 实施任务 / 设计冻结
> 最后核对：2026-08-24（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3E 变更
> 核对范围：标准部署入口、Compose profile/服务拓扑、schema job、现有 guarded-live preflight 与部署报告
> 运行时边界：只做静态、脚本替身与单元验证；未读取 `.env.*`，未连接 Docker/WSL2/数据库/交易所，未部署
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO；本阶段代码层硬禁用 live profile 部署**

## 1. 业务目标与边界

本阶段收口 `FS-007` 已确认的危险发布路径：部署入口默认选择真实资金 profile、应用启动失败后继续、衍生品 live-only collector 未纳入必需服务、浅层健康通过后打印“部署完成”，以及旧 app/schema/参数没有经过验证的一致回退契约。

当前全系统审计仍有跨进程 Kill Switch、关键任务 hang/lag、克隆库迁移/回滚、网络、容量和目标环境只读证据等未关闭项，因此不能在本阶段提供任何绕过开关允许真实资金发布。目标是先把标准入口改成默认拒绝：profile 必须显式提供，所有 live profile 在任何同步、构建、停服或数据库动作前非零退出。模拟盘仍可用于用户计划中的本地验证。

本阶段不宣称完成 production trading-readiness packet，不自动恢复 schema，不执行部署，也不改变交易策略、风控阈值或订单语义。

## 2. 当前行为与根因

修复前静态链路证明：

- `PROFILE="derivatives-live"`，遗漏 `--profile` 会进入真实资金配置；
- `step_app_up()` 捕获 Compose 非零，只写 warning 后继续；
- 基础设施 up 与 down 也容许部分失败后继续，可能形成混合拓扑；
- `derivatives-live` 的 required list 缺少 `aats-liquidations-daemon` 与 `aats-microstructure-collector`；
- 成功条件是 Gateway `/healthz` 加五个容器 health，没有 schema receipt、image identity、参数、恢复/对账、Kill Switch、command/outbox 或 live-only daemon 完整证据；
- 报告标题为“部署完成”，会把局部存活误读为可交易；
- 构建虽已在 Phase 3E 移到 down 之前，但旧镜像、schema 与参数没有经过克隆环境一致回滚演练。

根因是部署可用性检查、交易可用性判断和上线批准混在同一个成功文案里，同时对未知项采取继续而非失败关闭。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `scripts/deploy.sh` | 显式 profile、live NO-GO、步骤失败关闭、必需服务、部署证据包与准确报告语义 |
| Compose overlays | 当前 profile 的实际服务集合与镜像来源；不在本阶段改变交易行为 |
| schema job | 继续作为 app up 前 root + RDP apply/validate 真源；输出仅是部署证据的一部分 |
| guarded-live preflight | 保留为未来 runtime packet 的业务真源；本阶段不把静态调用或 HTTP 200 当作放行 |
| audit/docs | 区分“模拟部署基础检查通过”“trading-ready”“production GO”三种状态 |

部署结果模型至少区分 `blocked_before_mutation`、`failed`、`simulation_stack_healthy`。不存在本阶段可生成的 `production_ready` 状态。

## 4. 输入/输出接口

`scripts/deploy.sh` 接口收紧为：

- `--profile <spot|derivatives|spot-live|derivatives-live|derivatives-live-monolith>` 必填；
- 未提供 profile：exit 非零，且不得调用 WSL、Git 写操作、构建、停服或 schema；
- 任一 live profile：exit 非零并输出固定 NO-GO 原因；没有 `--yes`、环境变量或隐藏 override；
- 模拟 profile：沿用现有同步/构建/schema/app/health 流程；
- 成功时输出脱敏部署证据包路径和 `simulation_stack_healthy`，不得输出“已上线”“可交易”或“生产完成”。

证据包只包含非敏感字段：生成时间、WSL commit、profile、Compose overlay、image ID、schema job 状态、必需容器及其状态。不得包含 env 路径内容、DSN、用户名、密码、API key 或 token。

## 5. 数据库 schema、表、索引与约束

本阶段不新增或修改业务 schema。Phase 3E `scripts/apply_schema_migrations.py` 仍是唯一部署期迁移入口，root 与 RDP ledger/checksum 仍是 schema revision 真源。

部署证据包记录 `schema_job=passed` 只说明本次命令返回成功，不等于克隆库 manifest 相等、生产 schema 已验证或旧 app 可兼容回退。

## 6. 事务、一致性与并发

脚本步骤保持串行：显式 profile 与 live gate 必须发生在所有外部副作用之前；build 成功后才 down；infra 完整启动后才 schema；schema 成功后才 app；app up 非零立即失败，不能进入健康轮询并掩盖部分启动。

由于当前迁移不具备可证明的自动逆向事务，失败时不得自动 restore 数据库或把旧镜像重新启动后声称已一致回退。现场恢复必须保持人工批准和克隆验证前置；当前脚本的安全姿态是非零退出、保留状态与证据、禁止成功结论。

## 7. 授权、认证与数据安全

live gate 无 override，`--yes` 只处理已存在的“WSL checkout 与 Windows 工作区不同”确认，不得兼作实盘批准。脚本与证据包不读取或打印 `.env.*` 内容和凭据。

本阶段不调用 authenticated Operator API，不触发 halt/resume，不触发交易所请求。未来 runtime readiness 必须使用受控只读认证，不能新增公开的账户/风控状态 endpoint。

## 8. 错误处理与幂等

- profile 缺失、未知或 live：副作用前失败；
- build/down/infra up/schema/app up 任一非零：立即非零退出；
- required container missing/unhealthy 或 Gateway health 超时：非零；
- image identity、WSL commit 或证据包写入失败：不得报告成功；
- 重复模拟部署可重复产生新的时间戳证据包，不修改旧包；
- 失败路径不得使用 `|| true`/warning 吞掉关键步骤；仅只读报告展示可以 best-effort。

## 9. 状态转换与生命周期

```text
parse CLI
  -> profile missing/unknown: BLOCKED, no side effect
  -> live profile: PRODUCTION_NO_GO, no side effect
  -> simulation profile
       -> preflight/sync/build
       -> down (failure stops)
       -> infra up (failure stops)
       -> schema job (failure stops)
       -> app up (failure stops)
       -> gateway + every required container healthy
       -> write immutable-style evidence packet
       -> SIMULATION_STACK_HEALTHY only
```

重新开放 live profile 必须是后续独立任务，要求 G1–G8、完整 packet、克隆回滚演练与人工批准均有证据；不得仅删除本 gate。

## 10. 缓存与性能

不新增运行时缓存。部署证据只执行固定数量的 Git/Docker inspect，复杂度与必需容器数线性相关。健康轮询沿用有界超时。

证据目录保留策略由后续运维治理任务定义；本阶段采用按 UTC 时间戳命名，避免覆盖前次结果。

## 11. 日志、监控与审计

日志必须明确：profile、当前阶段、失败类别、模拟部署状态、runtime 未验证项。不得在 live 被拦截时生成“部署开始/完成”误导信息。

证据包需要可由测试校验字段完整性；image 记录使用不可变 ID，而非只记录可变 tag。生产 GO 仍只能由全系统 gate 与人工复核决定。

## 12. 测试策略

新增/更新静态和 shell 替身测试覆盖：

1. 不传 profile 失败且未调用 WSL；
2. 三个 live profile 均在副作用前失败，`--yes` 不能绕过；
3. `derivatives-live` future required list 包含两个 live-only collector；
4. app up、infra up、down 非零均不可继续；
5. build 仍在 down 前，schema 仍在 app 前；
6. 模拟成功报告不含 production/trading-ready 结论；
7. 证据包包含 commit/image/profile/schema/container 字段且不包含 credential key；
8. shell syntax、Ruff、focused/相关单测和全量 unit。

不会运行 WSL2/Docker、不会连接真实数据库/交易所；runtime/rollback 测试保持未验证。

## 13. 迁移、回滚与兼容

CLI 行为是有意的安全收紧：过去省略 profile 或使用 live profile 的命令会失败。所有自动化、wrapper 和现行文档必须显式使用 `spot` 或 `derivatives` 进行本地模拟测试。

本阶段不提供 live override，也不自动 app+schema rollback。若模拟部署在 schema 后失败，operator 必须把它视为未完成并按当前备份/恢复手册人工处置；没有克隆演练不得声称旧镜像与新 schema 兼容。

## 14. 配置与环境隔离

`spot`/`derivatives` 继续使用模拟 profile env；`spot-live`、`derivatives-live`、`derivatives-live-monolith` 只保留配置解析和未来验证用途，标准部署入口拒绝执行。

不得通过 `AATS_WSL2_*`、`--skip-sync`、`--skip-commit`、`--yes` 或直接 Compose 形成第二个 live 发布入口。文档继续声明 `scripts/deploy.sh` 是唯一入口，直接 Compose 禁止。

## 15. 代码组织与依赖

预计修改：

- `scripts/deploy.sh`：显式 profile、live gate、严格步骤、证据包和报告语义；
- `tests/unit/test_fs007_deployment_fail_closed.py` 与既有部署静态测试；
- prewarm/wrapper 中会自动触发 live 的默认值或拓扑映射；
- `DEPLOYMENT.md`、根/基础设施/Operations 入口与审计状态。

不新增第三方依赖，不修改应用 Python runtime、API、数据库模型或交易逻辑。

## 16. 文档、运维手册与验收标准

本阶段验收标准：

- 默认 live 路径不存在；profile 必填且 live 无 override；
- 所有关键 Compose 步骤失败关闭；
- future derivatives-live required list 已覆盖两个 collector；
- 模拟健康只输出准确的 `simulation_stack_healthy` 证据；
- 现行文档不再提供可执行 live 部署命令；
- focused、相关、全量 unit、Ruff、`bash -n` 和 Markdown 链接检查通过；
- 审计状态更新为 `RISK CONTAINED / LIVE DISABLED / READINESS & CONSISTENT ROLLBACK OPEN`；
- 真实资金继续 NO-GO。

最终关闭 FS-007 仍需：在隔离克隆环境实现并演练不可变 commit/image/schema/parameter packet；读回 execution kill generation 与所有 worker ack；验证 critical task identity/last-success/lag/dependency；账户/行情/持仓/对账新鲜且 command/outbox/unknown submission 为零；目标网络与容量通过；旧 app/schema/parameter 组合回滚满足 RTO/RPO；最后由独立人工复核。任何 unknown 仍按 NO-GO。
