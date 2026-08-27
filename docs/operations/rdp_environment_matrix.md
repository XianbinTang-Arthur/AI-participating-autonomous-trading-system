# RDP 环境策略矩阵

> 文档状态：现行专题参考（仅 `environment_guard` 策略层）
> 最后核对：2026-08-27（起始 HEAD `9c4112c6`，含当前控制面收口候选；以本文档所在 HEAD 为准）
> 核对范围：`aats/data_platform/operations/environment_guard.py` 与当前 API/部署门禁；不证明任何环境已经部署、可达或获准实盘
> 安全边界：所有 live profile 仍在标准 deploy/prewarm/wrapper 的副作用前硬拒绝，无 override

本页描述 RDP 业务策略如何解释 `dev/staging/prod`，不是一套可部署拓扑，也不是权限、
网络、数据库或真实资金授权。部署与端口只以根目录 [`DEPLOYMENT.md`](../../DEPLOYMENT.md)
和 `scripts/deploy.sh` 为准。

## 1. 环境身份解析

`get_current_environment()` 按以下顺序失败关闭：

1. 显式 `RDP_ENV` 只接受 `dev`、`staging`、`prod`；
2. managed profile 从 `AATS_PROFILE` / `AATS_ENV_TEMPLATE_PROFILE` 推导：
   `spot`、`derivatives` 对应 `staging`，`spot_live`、`derivatives_live` 对应 `prod`；
3. 显式值与 managed profile 不一致、两个 profile identity 冲突、未知 profile，或只出现
   `AATS_STARTUP_PROFILE` 的半初始化状态，均抛错而不是退回 `dev`；
4. 只有完全没有 managed identity 的隔离开发进程才默认 `dev`。

策略中的 `prod` 不等于“live 已开放”。当前 live profile 即使能被静态配置识别，也不能由
标准发布入口启动。

## 2. 当前策略值

| 策略 | dev | staging | prod |
| --- | --- | --- | --- |
| 允许 release 内部参数 apply | 是 | 是 | 是 |
| 允许参数 rollback | 是 | 是 | 是 |
| 允许 workflow execution | 是 | 是 | 是 |
| release 必须运行 gate | 否 | 是 | 是 |
| recommendation 必须已批准 | 否 | 否 | 是 |
| 策略层允许 direct DB access | 是 | 是 | 否 |
| release 最短 observation window | 0h | 24h | 72h |

这些 boolean 只是单层策略输入：

- direct `POST /rdp/parameters/apply` 已在所有环境停用并固定无写入返回
  `code=release_required`；表中的 apply 指 canonical release 内部 apply；
- `skip_apply=false` 的 `releases/create` 与 `approve-and-release` 仍需认证、Step2、
  promotion qualification、gate、短时 `apply` token、mapping、history 和 pending-risk veto；
- Operator `parameters/rollback` 仍需独立 `rollback` token、合法 target 与事务校验；
- 启用的 observation cycle 内部风险收敛不使用浏览器 token，但必须满足 exact
  post-apply provenance、combo lock、clean attempt 和数据库 action proof；
- `allow_workflow_execution=true` 不会解除 workflow 自身的 disabled/freeze。当前
  `decision_cycle` disabled，`release_cycle` disabled 且禁止入队；
- `allow_direct_db_access` 不是数据库 IAM 或网络 ACL。实际访问仍受部署、角色、凭证和
  Operator 纪律约束，禁止手工改治理状态。

## 3. Release 守卫

`guard_release_creation()` 解析 observation window，并执行：

- 负窗口一律拒绝；
- staging/prod 使用 `skip_gate=true` 一律拒绝；
- 窗口小于环境下限一律拒绝；
- 通过只表示环境策略层允许继续，不表示 recommendation、资格、gate、数据库或 runtime 已通过。

Managed recommendation/effectiveness 的 PostgreSQL 读取失败时禁止退回陈旧 JSON。
数据库 CAS 已提交但审计镜像刷新失败时，canonical 状态保持成功并单独报告 degraded；不得
重复提交状态迁移。

## 4. 当前部署边界

| Profile | RDP 策略身份 | 标准部署状态 | 应用容器要求 |
| --- | --- | --- | --- |
| `spot` | staging | 允许本地模拟 | gateway、market、decision、execution、rdp-daemon |
| `derivatives` | staging | 允许本地模拟 | 上述五个 + liquidations-daemon + microstructure-collector |
| `spot-live` | prod | **硬禁用** | 不得启动 |
| `derivatives-live` | prod | **硬禁用** | 不得启动 |
| `derivatives-live-monolith` | prod | **硬禁用** | 不得启动 |

模拟部署健康必须继续写 `production_ready=false`、`trading_ready=false`。它不证明数据覆盖、
候选收益、账户一致、真实成交或 live 可用。

## 5. 验证入口

- 策略与负向契约：相关 `tests/unit/test_rdp_production_hardening.py`；
- staging 业务演练：[`rdp_staging_rehearsal_checklist.md`](rdp_staging_rehearsal_checklist.md)；
- 参数与风险收敛：[`parameter_apply_and_rollback.md`](parameter_apply_and_rollback.md)；
- 唯一部署入口：[`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)。

任何现场操作前必须重新读取当前 profile、runtime、数据库和安全门；不得凭本矩阵的静态
“允许”单元格执行 live、真实订单或参数应用。
