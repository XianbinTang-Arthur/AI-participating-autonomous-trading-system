# FS-021 仓库级 CI 与 Warning 预算设计和实施范围

> 文档状态：Phase 3S 基础门禁已实施；远端运行、required check、integration 与供应链门禁开放
> 最后核对：2026-08-25（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3V 整改；本文件主体记录 Phase 3S，Phase 3T 供应链覆盖见文末
> 核对范围：GitHub Actions、Python 测试/lint 依赖、pytest markers/warnings、LongShort poller 单测 mock 与贡献文档
> 运行时边界：不读取 `.env.*`，不连接数据库、Redis、NATS、交易所或账户，不启动服务、Docker 或 WSL2，不部署
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段修复 `FS-021` 的基础层：建立仓库级 pull request/push CI，使全仓 Ruff、unit test、
strict markers 和新增 warning 自动阻断合并；清除当前 LongShort poller 单测制造的未 await
协程 warning，并为明确的 SQLite/Python 3.12 上游弃用噪声建立精确临时 allowlist。

本阶段不运行 live profile，不在 CI 注入 secrets，不启动 Compose，也不把 PostgreSQL/
NATS/Redis integration、Node/browser、secret/CVE/license scan 伪装成已完成。这些保留为
FS-021/022 后续门禁。

## 2. 整改前行为与根因

仓库没有 `.github/workflows` 或其他 pipeline，任何 lint/test 失败都依赖本地人员发现。
Phase 1 原 Ruff 的 9 个错误已被后续工作区清除，当前 `ruff check .` 可通过，但没有自动
执行者。全量 unit 仍有约 1,660 条相同 SQLite datetime adapter deprecation，以及 6 条
LongShort 测试把同步 `httpx.Response.raise_for_status()` 配成 `AsyncMock` 产生的
“coroutine was never awaited”。

根因是缺少持续门禁、warning 分类预算和真实接口形态的 mock contract。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `.github/workflows/quality.yml` | 只读 checkout、Python 3.12、安装测试依赖、全仓 Ruff、strict unit/warning gate |
| `pyproject.toml` | 明确 pytest/ruff 开发依赖与 pytest marker 注册 |
| LongShort poller tests | 用同步 `Mock` 表示真实同步 response 方法，保留 async client.get |
| FS-021 contract tests | 静态证明 workflow 不含 live/deploy/secrets/continue-on-error，Actions 固定 SHA |
| contributor/testing docs | 说明 CI 覆盖和仍未覆盖的门禁，不把 workflow 文件存在写成远端已运行 |

## 4. 输入/输出接口

CI 触发：

- pull request；
- push 到 `main`；
-人工 `workflow_dispatch`。

输出是 GitHub job 成败和标准测试日志，不生成部署 artifact，不写外部系统。失败必须保留
非零状态；禁止 `continue-on-error`、`|| true` 或失败后伪成功步骤。

## 5. 数据库 schema、表、索引与约束

无数据库或 migration 变更。CI unit job 不设置任何 DB URL，不开启 integration 环境门。

## 6. 事务、一致性与并发

workflow 使用 concurrency group；同一 ref 的新运行取消旧运行，避免并行浪费。取消只影响
无外部副作用的 lint/unit job，不影响任何部署或持久化事务。

## 7. 授权、认证与数据安全

- workflow 顶层权限只有 `contents: read`；
- checkout 禁用 persisted credentials；
- 不使用 `pull_request_target`、repository/environment secrets 或 write permission；
- 不读取 `.env.*`，不执行部署、Operator API 或交易调用；
- 第三方 Actions 固定到官方 release 的完整 commit SHA，并保留版本注释。

## 8. 错误处理与幂等

安装、Ruff 或 unit 任一步非零即 job 失败。unit 使用 `-W error`，只对精确匹配的 Python
3.12 SQLite datetime adapter deprecation 临时忽略；其他新增 warning 自动失败。

workflow 重跑不写仓库或外部状态，具有幂等性。

## 9. 状态转换与生命周期

```text
event
  -> read-only checkout
  -> Python 3.12
  -> verify FS-022 dependency contract
  -> install test/lint hashed lock
  -> ruff check .
  -> pytest unit + strict markers + warning error budget
  -> success or non-zero failure
```

不存在 deploy、service start、artifact promotion 或 live state transition。

## 10. 缓存与性能

本阶段不启用 pip cache，避免扩大缓存复现与污染边界。job 设置明确 timeout。Phase 3S
最初的开放依赖安装已在 Phase 3T 由 FS-022 hashed lock 替换；缓存仍未启用。

## 11. 日志、监控与审计

GitHub job log 提供命令与失败位置；不打印环境秘密。仓库审计只证明 workflow 代码和本地
等价命令通过，不能声称远端分支保护已经启用或某次 GitHub run 已成功。

## 12. 测试策略

新增对抗测试至少覆盖：

1. workflow 触发器、只读权限、timeout/concurrency；
2. 官方 Actions 固定 40 位 SHA，checkout 不持久化凭据；
3. Python 3.12、全仓 Ruff、unit、strict markers、`-W error` 与精确 SQLite allowlist；
4. 没有 live/deploy/env/secrets/write permission/continue-on-error；
5. test/lint extras 明确包含 pytest、pytest-asyncio、nats-py 和 pinned Ruff；
6. LongShort mock 的 `raise_for_status` 是同步 `Mock`，相关测试零 warning。

运行 focused、LongShort、warning-strict unit、全仓 Ruff 与全量 unit。

## 13. 迁移、回滚与兼容

新增 workflow 不改变运行时 API。若 CI 环境暴露 Linux/Python 3.12 差异，应修复代码或
明确缩小错误 allowlist，不能删除 gate。紧急回滚只可临时撤回 workflow 文件，并必须在
风险登记簿恢复 `VERIFIED`；不得保留文档中的“已自动阻断”表述。

## 14. 配置与环境隔离

CI 仅使用仓库默认的纯 unit 路径，不设置 `AATS_RUN_*_INTEGRATION`，不加载 managed
profile。Python 版本与 Docker target 3.12 对齐；Windows 本地 temp ACL 不是 CI allowlist。

## 15. 代码组织与依赖

预计修改：

- 新增 `.github/workflows/quality.yml`；
- `pyproject.toml` 的 test/lint extras 与 pytest marker 配置；
- `tests/unit/test_long_short_poller.py`；
- 新增 `tests/unit/test_fs021_ci_quality_gate.py`；
- CONTRIBUTING、测试指南、SOW 与全系统审计。

不新增应用运行时依赖；测试/开发依赖不会进入默认 `aats` 安装。

## 16. 文档、运维手册与验收标准

本阶段验收：

- 当前仓库存在最小权限、无 secrets/live/deploy 的自动 quality workflow；
- `ruff check .`、strict marker collection、focused strict-warning 与标准完整 unit 通过；
- 远端 Python 3.12 workflow 仍须实际运行；本机 Python 3.14 的全量 `-W error` 会额外
  暴露 SQLite connection finalizer `ResourceWarning`，不能伪装成 3.12 CI 结果；
- LongShort AsyncMock warning 消失，SQLite allowlist 精确且明确为临时技术债；
- focused、related、full unit、Ruff、workflow YAML、文档链接和 diff check 通过；
- FS-021 更新为基础门禁代码已收口，但远端运行/分支保护/integration/security scan 开放；
- FS-022 已部分整改为 hashed lock/image digest，但 APT、SBOM/扫描、clean build 和远端
  治理继续 OPEN；真实资金生产继续 NO-GO。

最终关闭 FS-021 仍需在 GitHub 远端启用 required status check/branch protection，并补齐
隔离 PostgreSQL/NATS/Redis integration、Node/browser、schema/Compose、secret scan 和
warning allowlist 清零计划；本地静态检查不能替代远端治理状态。

实施与验证证据见
[`../../audit/full_system_2026_08_24/39-fs-021-ci-quality-gate.md`](../../audit/full_system_2026_08_24/39-fs-021-ci-quality-gate.md)。

> Phase 3T 后续覆盖：CI 现在先运行 `scripts/verify_dependency_locks.py`，再按
> `requirements/ci-py312-linux-x86_64.lock` 的版本/hash 安装。该变更不改变 FS-021
> 的远端 required-check 与 integration 开放状态；供应链边界见
> [`fs_022_reproducible_dependencies_sow_2026_08_25.md`](fs_022_reproducible_dependencies_sow_2026_08_25.md)。
