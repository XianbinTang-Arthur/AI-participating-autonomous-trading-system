# FS-021 仓库级 CI 质量门禁整改证据

> 文档状态：Phase 3S 基础门禁已实施；Phase 3T 已接入 hashed CI lock；远端运行、required check、integration 与完整供应链门禁开放  
> 最后核对：2026-08-25  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：`codex/fs-002-kill-switch-p0` 上未提交 Phase 3A–3V 叠加变更；本记录主体证据止于 Phase 3T  
> 运行时边界：未读取 `.env.*`，未连接数据库、Redis、NATS、交易所或账户，未启动服务、Docker 或 WSL2，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 整改前事实

Phase 1/2 审计时仓库没有 `.github/workflows` 或其他持续集成入口，Ruff 为非零；完整
unit 虽通过，但存在 1,665 条 warning。后续 Phase 3A–3R 已把 Ruff 修到通过，warning
增长为 1,666 条，其中绝大多数是 SQLite datetime adapter 弃用告警，另有 6 条来自
`test_long_short_poller.py`：测试把同步的 `httpx.Response.raise_for_status()` 建模为
`AsyncMock`，调用后产生未 await 协程 warning。

这意味着本地回归无法自动约束合并，新增 warning 也会被既有噪声淹没。`FS-021` 的
finding 成立；“Ruff 非零”仅是历史快照，实施前当前工作区的 Ruff 已通过。

## 2. 本阶段实现

### 2.1 最小权限基础 CI

新增 `.github/workflows/quality.yml`：

- `pull_request`、`main` push 和人工 `workflow_dispatch` 触发；
- `ubuntu-24.04`、Python 3.12，与当前 Docker Python 主版本一致；
- workflow 权限仅 `contents: read`，checkout 禁止 persisted credentials；
- `actions/checkout` 与 `actions/setup-python` 固定到完整 40 位 release commit SHA；
- Phase 3T 起先运行 dependency verifier，再从 CI hashed lock 安装，随后执行
  `ruff check .` 和完整 `tests/unit/`；
- unit 启用 `--strict-markers`、禁用 pytest cache，并把新增 warning 作为错误；
- 只对精确匹配的 Python 3.12 SQLite 默认 datetime adapter 弃用消息保留临时 allowlist；
- 设置同 ref concurrency cancellation 和 20 分钟超时；
- 不使用 secrets、`.env`、Docker、部署命令、`pull_request_target`、write permission、
  `continue-on-error` 或 `|| true`。

该 workflow 无任何实盘或外部系统副作用。文件存在只证明仓库定义已实现，不能证明
GitHub 远端已经运行成功、分支保护已经要求该检查，或有人无法绕过合并门。

### 2.2 工具与 marker 契约

`pyproject.toml` 明确声明：

- `pytest>=8,<10`、`pytest-asyncio>=0.23,<2`、`nats-py>=2.7,<3`；
- lint extra 固定 `ruff==0.15.8`；
- 注册 `asyncio` 与 `integration` marker，使 unit collection 可使用
  `--strict-markers` 失败关闭。

Phase 3S 实施时应用/CI 依赖仍未 lock/hash；Phase 3T 已用目标平台 hashed lock 替换该
路径。APT、SBOM、扫描、clean build 与远端运行仍未完成，详见 `40`，因此仍不能写成
完整供应链可复现。

### 2.3 Warning 根因修复

Long/Short poller 测试保留 async client `get()` 的 `AsyncMock`，但将真实同步 response
方法 `raise_for_status()` 改为 `Mock`。这修复 mock contract，而不是隐藏 warning。
标准全量 warning 从 1,666 降为 1,660；剩余条目均是同一 SQLite datetime adapter
deprecation 类别。

## 3. 对抗契约

新增 `tests/unit/test_fs021_ci_quality_gate.py`，静态锁定：

1. 触发器、只读权限、runner、timeout 和 concurrency；
2. 两个 Action 的完整 SHA、Python 3.12 和 checkout credential 边界；
3. verifier、hashed lock 安装、全仓 Ruff、完整 unit、strict markers、warning error 与
   精确 allowlist；
4. 禁止 live/deploy/secrets/env/write/fail-open 片段；
5. test/lint extras 的明确范围；
6. pytest marker 注册；
7. `raise_for_status` 必须使用同步 `Mock`。

## 4. 验证结果

| 检查 | 结果 | 可信边界 |
|---|---|---|
| FS-021 + Long/Short focused | `17 passed` | 本机 Python 3.14；普通模式 |
| 同一 focused strict warning | `17 passed` | `-W error`，仅 SQLite datetime 精确 allowlist；0 warning |
| 全仓 Ruff | `All checks passed!` | `.venv` 中 Ruff 0.15.8 |
| unit strict marker collection | `4405 tests collected` | `--strict-markers`，无 unknown unit marker |
| 标准完整 unit | `4375 passed, 30 skipped, 1660 warnings, 85 subtests passed in 107.96s` | 仓库内唯一 basetemp；无断言失败 |

本机 `.venv` 实际是 Python 3.14.0，而 workflow/生产镜像目标是 Python 3.12。对全量 unit
直接使用 workflow 的 `-W error` 时，在 339 项处由 Python 3.14 的 SQLite connection
finalizer `ResourceWarning` 中止：`338 passed, 1 failed`。这不是业务断言失败，也不是
workflow Python 3.12 的实跑结果；本阶段没有扩大 workflow allowlist 去隐藏 3.14
资源告警。远端 Python 3.12 workflow 尚未运行，所以不得声称 CI 已绿色。

标准完整 unit 的 1,660 条 warning 均来自测试 SQLite datetime adapter 弃用路径；
workflow 用精确消息 allowlist 将该既有预算隔离，其他 warning 仍失败。该 allowlist 是
显式技术债，不是“零 warning”结论。

## 5. 关闭标准与残余风险

当前裁定：`PARTIALLY REMEDIATED / BASE CI GATE ADDED / REMOTE ENFORCEMENT & INTEGRATION OPEN`。

已收口的是仓库内基础 lint/unit 自动门禁定义、strict marker、Long/Short 错误 mock 和
新增 warning 阻断路径。以下仍开放：

- GitHub 远端实际运行及日志证据；
- branch protection/ruleset 的 required status check 和管理员绕过治理；
- PostgreSQL/NATS/Redis 隔离 integration、Compose/schema/config、Node/browser；
- secret、CVE/license、SBOM、APT snapshot 与 artifact provenance；
- Python 3.14 SQLite test engine 生命周期兼容；
- SQLite datetime allowlist 的根因清零；
- FS-022 的 clean build、远端消费、扫描、签名与 provenance 闭环。

因此 `FS-021` 尚未 CLOSED，`FS-022` 为部分整改而非 CLOSED，所有真实资金上线 gate
继续 NO-GO。本记录不授权部署、实盘验证或修改远端仓库设置。Phase 3T 证据见
[`40-fs-022-reproducible-dependencies.md`](40-fs-022-reproducible-dependencies.md)。
