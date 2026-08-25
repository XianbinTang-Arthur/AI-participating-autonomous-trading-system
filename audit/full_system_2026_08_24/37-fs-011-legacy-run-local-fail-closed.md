# 37 FS-011 Legacy `run_local.py` 失败关闭记录

> 文档状态：现行整改证据  
> 阶段：Phase 3Q  
> 核对日期：2026-08-25  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作分支：`codex/fs-002-kill-switch-p0`，变更尚未提交  
> 验证边界：legacy CLI、当前 decision process 签名、subprocess 与 Windows 单元测试；未启动 runtime  
> 安全边界：未读取 `.env.*`，未连接数据库、Redis、NATS、交易所或账户，未启动容器，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 结论

Phase 3Q 消除了 `FS-011` 的失效启动旁路。整改前 `scripts/run_local.py` 会先加载
profile dotenv，再把当前无参同步 `apps.decision_engine.main()` 当作接受
`iterations`/`interval_seconds` 的协程调用，必然在运行时抛 `TypeError`。

旧路径现在保留为明确迁移失败入口：只解析旧参数，不导入 asyncio、profile loader 或
decision runtime，不读取 `.env.*`，向 stderr 输出当前 API/UI 与有限迭代测试迁移指引，
固定返回 exit code `2`。它不会自动启动 `start_api.py`，避免旧命令产生新的长生命周期
副作用。

当前裁定：

**FS-011：CODE REMEDIATED / LEGACY ENTRY FAIL-CLOSED / EXTERNAL CALLER MIGRATION OPEN**。

## 2. 实施内容

`scripts/run_local.py` 当前契约：

- `--iterations`、`--interval-seconds`、`--profile {spot,derivatives}` 仍可解析，以便旧调用
  得到可操作的迁移说明；
- 无参数或合法旧参数都输出同一 UTF-8 中文说明并返回 `2`；
- live profile 与未知参数仍由 argparse 拒绝；
- 不修改无参同步 decision process entry 来迁就遗留 caller；
- 本地 API/UI 指向 `scripts/start_api.py --profile derivatives`；有限迭代闭环指向明确选择
  的 `tests/integration` 场景，不声称存在新的通用 paper-loop CLI。

这保留了路径可发现性，但不保留错误的成功预期。任何仓库外脚本若依赖旧 JSON summary，
都会确定性非零失败，必须显式迁移。

## 3. 防御性验证

新增 `tests/unit/test_fs011_legacy_run_local_fail_closed.py` 六项测试，覆盖：

1. 源码没有 asyncio、dotenv loader 或 decision-main 导入；
2. 无参数调用返回 2，stdout 为空，stderr 含两类迁移指引和无 dotenv 声明；
3. 全部旧合法参数被识别但仍失败关闭；
4. `derivatives_live` 在迁移消息前由 argparse 拒绝；
5. 真 subprocess 运行无需 `.env.*`，固定 exit 2 且无 stdout；
6. decision process main 保持同步、无参数签名。

## 4. 测试记录

```text
focused: 6 passed, 1 warning in 0.98s
related isolated basetemp: 51 passed, 1 warning in 2.70s
targeted Ruff: All checks passed!
application Ruff: All checks passed!
```

相关组合首次不指定 basetemp 时为 `47 passed, 4 errors`；四个 error 都发生在 pytest
创建 Windows 系统 `tmp_path` 前，隔离 basetemp 后 51 项全通过。

仓库规定的原样全量命令：

```text
87 passed, 2 warnings, 1 error in 3.55s
```

唯一 error 同样是系统临时目录 `PermissionError [WinError 5]`，此前无断言失败。仓库内
全新 basetemp 完整结果：

```text
4351 passed, 30 skipped, 1666 warnings, 85 subtests passed in 108.36s
```

warning 仍主要来自既存 SQLite datetime adapter、LongShort poller AsyncMock 与
`.pytest_cache`，由 FS-021 承接。

文档与差异检查：`90` 个变更/新增 Markdown、`390` 个本地链接、`broken=0`；
`git diff --check` exit `0`，仅有既存 CRLF 转换提示。

## 5. 未执行验证与关闭条件

未执行 WSL2 integration、服务启动、Docker、数据库、Redis、NATS 或交易所操作；没有
读取 `.env.*`。当前变更未提交，因此没有在 committed candidate 上运行旧命令。

最终关闭 FS-011 还需：

1. 独立 reviewer 在 committed candidate 上运行无参数、旧参数和非法 live 参数矩阵；
2. 盘点仓库外任务、快捷方式和操作手册，迁移仍期待 JSON summary/exit 0 的调用方；
3. 若需要新的有限迭代 CLI，围绕当前 runtime composition 与安全生命周期另建设计，
   不能恢复对 decision process main 的错误调用。

## 6. 当前裁定

已收敛：签名 TypeError、失败前 dotenv 注入、旧路径看似是可运行 paper loop，以及通过
修改核心 process entry 兼容遗留 caller 的风险。

未收敛：committed candidate 独立复核与仓库外调用方迁移。

**FS-011：CODE REMEDIATED / LEGACY ENTRY FAIL-CLOSED / EXTERNAL CALLER MIGRATION OPEN。**  
**REAL-MONEY PRODUCTION：NO-GO。**
