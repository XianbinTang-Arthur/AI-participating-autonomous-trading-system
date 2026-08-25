# 26 FS-007 部署失败关闭与实盘入口隔离整改

> 核对日期：2026-08-24  
> Git 基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：`codex/fs-002-kill-switch-p0` 上的未提交 Phase 3A–3F 叠加变更  
> 当前裁定：`RISK CONTAINED / LIVE DISABLED / READINESS & CONSISTENT ROLLBACK OPEN`  
> 上线决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 范围与安全边界

本阶段只整改标准 WSL2 部署入口、其生命周期包装器、模拟部署证据和现行文档，目标是先阻断意外实盘与错误成功。没有读取或展示 `.env.*`，没有连接交易所、账户或数据库，没有执行 WSL2/Docker 部署，也没有查询余额、订单、持仓、活动参数、Kill Switch 或对账状态。

现阶段不能诚实构造完整 production trading-readiness packet，也不能证明 app、schema 与参数的一致回滚。因此没有提供临时实盘 override；`--yes` 也不能绕过实盘硬禁用。允许的仅是用户显式选择 `spot` 或 `derivatives` 模拟 profile，用于后续本地测试。

## 2. 修复前已验证路径

Phase 1/2 与 Phase 3E 后的静态追踪确认：

1. `scripts/deploy.sh` 无参数时默认选择 `derivatives-live`；
2. app `docker compose up` 非零可被警告后继续，旧栈 `down` 失败也可被吞；
3. infrastructure up 没有以 Compose health wait 作为硬门；
4. live required-container 清单遗漏 liquidations 与 microstructure 两个 live-only collector；
5. 最终“完成”主要基于浅层 HTTP/container health，不证明账户/行情新鲜度、recovery、reconciliation、Kill Switch generation/ack、活动参数、command/outbox 或 exchange mode；
6. 没有不可变部署身份记录，也没有经演练的旧 image + schema + parameter 一致回滚。

这些事实共同允许“错误 profile、关键步骤失败或关键状态未知”仍被操作员理解为可上线。

## 3. 本阶段实施

### 3.1 实盘路径硬隔离

- `scripts/deploy.sh` 不再设置默认 profile；缺少 `--profile` 时在任何 WSL、Docker、数据库或同步动作前以非零退出。
- `spot-live`、`derivatives-live` 与 `derivatives-live-monolith` 在同一最前置门禁被拒绝；无 override，`--yes` 不改变结果。
- `run-deploy.ps1`、prewarm 与 startup-task 创建入口默认改为模拟 `derivatives` 并拒绝启动 live。
- keepalive 只拒绝 live `Start`，保留 `Stop`/`Status`；startup-task 只拒绝 live 注册，保留 `Remove`，避免阻断遗留风险清理。

### 3.2 关键部署步骤失败关闭

- 顺序固定为 preflight/sync → build → down → infrastructure up/wait → schema job → app up → health → evidence。
- build 在 down 前完成；down、infrastructure up 与 app up 的非零状态不再被吞。
- infrastructure 使用 Compose `--wait` 与有界 timeout；schema job 必须在 app up 前成功。
- future live topology 的 required-container contract 同时列出 liquidations 与 microstructure collector。该清单是未来验收契约，不代表 live 已启用。

### 3.3 诚实的模拟部署证据

新增 `scripts/write_deployment_evidence.py`，只接受 `spot`/`derivatives`：

- 记录 UTC 时间、40 位 commit、base image digest、profile/overlay、schema job 状态和每个必需容器的状态/health/image digest；
- 任一必需容器不是 `running` 且 `healthy`、commit/image 身份格式无效或 profile 为 live 时失败；
- 明确写入 `production_ready=false`、`trading_ready=false`；
- 明确列出没有验证的 schema clone/rollback、账户/行情、Kill Switch、参数、recovery/reconciliation、网络与容量边界；
- 使用唯一文件名、排他创建、`fsync` 和只读权限，运行产物目录不进入 Git。

部署成功文案相应改为“模拟栈基础检查通过”，不得解释为 trading-ready 或生产放行。

## 4. 机器验证

当前已完成：

| 检查 | 结果 |
|---|---|
| FS-007 独立对抗文件 | `11 passed`；1 个既有 pytest cache 权限 warning |
| 首轮 deploy focused 组 | `34 passed`；1 个既有 pytest cache 权限 warning |
| deploy/process/startup/FS-007/FS-009 相关回归 | `73 passed`；1 个既有 pytest cache 权限 warning |
| README/FS-007 文档契约 | `17 passed`；1 个既有 pytest cache 权限 warning |
| 最终全量 unit | `4197 passed, 30 skipped, 1666 warnings, 85 subtests passed in 96.41s` |
| deploy shell 语法 | `bash -n scripts/deploy.sh` 通过 |
| sync shell 语法 | `bash -n scripts/sync_to_wsl2.sh` 通过 |
| Ruff `aats apps scripts tests` | `All checks passed`；并机械清理 9 个既有测试 lint 问题 |
| PowerShell parser | 4 个 lifecycle/deploy wrapper 全部通过 |
| PowerShell lifecycle dry-run | legacy live Stop/Remove 成功；live Start 按预期拒绝 |
| 变更 Markdown 相对链接 | 60 个文件通过 |
| Git whitespace/diff | `git diff --check` 通过；仅有既有 LF/CRLF checkout 提示 |

首轮测试曾因 Windows Git Bash 绝对路径转换与系统临时目录权限失败；改为仓库相对脚本路径并把 pytest basetemp 放入审计目录后通过。首次全量 unit 在 `2144 passed` 后因旧 README 测试仍强制要求展示 live HTTPS 地址而停止；当前文档已硬禁用 live，该断言改为验证“模拟 HTTP 明确 + live 禁用/无 override 明确”，相关 17 项与从头重跑的全量 unit 随后通过。上述过程是测试/文档契约纠正，不计作未发生的产品成功，也没有被省略。

全量 warnings 未隐藏：主要是既有 SQLite datetime deprecation、LongShort poller AsyncMock 未 await 和 pytest cache 权限 warning。本阶段没有执行 WSL2/Docker/数据库 integration；这些静态与隔离结果不得外推为运行态通过。

## 5. 已关闭、未关闭与未知

### 静态/隔离已关闭

- 无 profile 意外进入 live；
- 标准部署、prewarm、startup 与 keepalive 启动 live；
- `--yes` 绕过 live 禁令；
- down/infrastructure/app up 非零后继续；
- future derivatives-live required list 遗漏两个 collector；
- 模拟基础健康被文案或 evidence 标记为 production/trading ready。

### 仍未关闭

- 完整 trading-readiness packet 的账户/行情/仓位新鲜度、recovery、reconciliation、Kill Switch generation/ack、活动参数、command/outbox、exchange mode；
- FS-006 的永久 hang/lag 与 dependency freshness；
- FS-009 的 clone manifest 与真 PostgreSQL 前滚/部分失败/回滚；
- 旧 app image + schema + parameter 的一致、可恢复 rollback；
- 目标网络/TLS/证书/防火墙与数据库/队列/内存容量；
- 生产等价克隆环境的部署失败矩阵、rollback drill 和独立人工复核。

### 运行时 UNKNOWN

当前实际容器、部署 commit/image、数据库 revision、账户、交易所模式、资金、订单、活动参数、Kill Switch、对账与告警均未在本阶段读取。静态禁用 live 不等于确认任何既有外部环境已经停机；如需处理遗留进程，应使用受控的 Stop/Remove 路径并另行留证。

## 6. Finding 与 Gate 裁定

FS-007 仍是 P1 HARD BLOCKER，但风险姿态从原始 `CONFIRMED/FAIL` 更新为：

```text
RISK CONTAINED / LIVE DISABLED / READINESS & CONSISTENT ROLLBACK OPEN
```

G5 仍为 `PARTIAL / 未放行`，而不是 PASS。理由是本阶段通过关闭 live 入口消除了从标准工具意外进入实盘的路径，并改善了模拟部署的失败关闭和身份证据；但没有实现或演练生产 readiness 与一致回滚。

## 7. 后续关闭条件

只有至少完成以下工作，才可另行评估是否重新开放 live：

1. 先在隔离克隆环境实现完整、不可变且任一 UNKNOWN 都失败的 trading-readiness packet；
2. 注入 build/down/infra/schema/app/collector/freshness/recovery/reconciliation/kill/parameter/backlog 各类失败；
3. 保留并验证上一 app image，演练 app + schema + parameter 一致回退，记录 RTO/RPO；
4. 完成 FS-002、FS-001、FS-006、FS-009 及网络/容量前置门禁；
5. 由独立 reviewer 核对代码、隔离运行证据与目标环境只读证据；
6. 获得新的明确人工批准后，才允许设计 live 解禁机制。

本阶段不提供解禁命令，也不构成部署、上线或真实资金操作授权。
