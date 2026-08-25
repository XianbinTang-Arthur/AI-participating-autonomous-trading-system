# FS-011 Legacy `run_local.py` 失败关闭设计与实施范围

> 文档状态：Phase 3Q 已实施；外部调用方迁移与独立复核开放  
> 最后核对：2026-08-25（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3P 整改  
> 核对范围：`scripts/run_local.py`、当前 decision process 入口、现行启动/测试文档与相关单测  
> 运行时边界：未读取 `.env.*`，未连接数据库、Redis、NATS、交易所或账户，未启动服务、Docker 或 WSL2  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段修复 `FS-011`：保留旧脚本路径作为明确的迁移告警入口，但禁止它继续按已经
不存在的异步 decision-engine 函数契约加载 profile 或尝试启动 runtime。

本阶段不恢复单进程有限迭代 paper loop，不把四进程架构重新塞回 legacy runner，也不
自动转调 `start_api.py`。自动启动长生命周期服务会扩大旧命令的副作用，必须另行设计。

## 2. 整改前行为与根因

`scripts/run_local.py` 加载 `.env.<profile>` 后执行：

```python
asyncio.run(main(iterations=..., interval_seconds=...))
```

但 `apps/decision_engine/main.py::main()` 当前是无参数同步函数，返回 process exit code，
并通过 `run_process_sync(process_role="decision")` 启动一个长期 decision slice。旧脚本会
在调用处立即抛 `TypeError`，而且在报错前已经把 profile dotenv 注入当前进程。

根因是单进程 paper loop 被四进程 runtime 架构替代后，遗留 CLI 没有同步删除、迁移或
增加失败关闭契约。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `scripts/run_local.py` | 接受旧参数以识别迁移场景；不加载配置、不导入 runtime；输出唯一迁移说明并非零退出 |
| `scripts/start_api.py` | 当前受支持的本地 API/UI 模拟 profile 入口；不由旧脚本自动调用 |
| `apps/decision_engine/main.py` | 保持无参同步 process entry，不为 legacy runner 恢复 kwargs |
| integration tests | 有限迭代内存闭环的测试载体；不是生产或通用 CLI |

## 4. 输入/输出接口

旧参数名 `--iterations`、`--interval-seconds`、`--profile {spot,derivatives}` 保留为可解析
输入，避免用户只看到“未知参数”。无论是否提供这些参数，正常解析后都：

- 向 stderr 输出 UTF-8 中文迁移说明；
- 明确指出本地 API/UI 使用 `scripts/start_api.py --profile derivatives`；
- 明确指出有限迭代闭环应走受控 integration scenario；
- 返回 exit code `2`。

`--help` 仍由 argparse 返回标准帮助；未知参数或非法 profile 仍由 argparse 拒绝。

## 5. 数据库 schema、表、索引与约束

无数据库 schema、migration、table、index 或 constraint 变更。旧入口在任何数据库
配置读取或连接前退出。

## 6. 事务、一致性与并发

无事务、asyncio task 或共享并发状态。失败路径是单进程、确定性、无配置副作用的同步
退出；不创建 event loop，不启动 background task。

## 7. 授权、认证与数据安全

不读取 `.env.*`、secret、账户或认证数据，不导入 profile loader。错误消息只包含固定
迁移指引，不回显参数值以外的环境信息。

## 8. 错误处理与幂等

- 旧参数合法：固定 exit `2` 与同一迁移说明；
- 参数非法：argparse 固定 exit `2`；
- 重复执行：无文件、网络、进程或数据库状态变更；
- 不捕获并伪装受支持入口的错误，因为本入口不会调用它们。

## 9. 状态转换与生命周期

```text
legacy command invoked
  -> parse legacy arguments only
  -> print migration guidance to stderr
  -> exit 2

No dotenv load -> no runtime import -> no event loop -> no service start
```

## 10. 缓存与性能

无缓存。脚本只导入标准库并解析参数，退出延迟与资源占用可忽略。

## 11. 日志、监控与审计

stderr 文案是本地迁移诊断，不写应用日志或审计表。审计记录保留原 TypeError 事实和修复
后明确失败语义；不得将“入口可给指引”写成“paper loop 已恢复”。

## 12. 测试策略

新增 FS-011 回归测试：

1. 源码不再导入 asyncio、dotenv loader 或 decision main；
2. `main([])` 返回 2，stderr 同时给出 API 与有限迭代测试指引；
3. 全部旧合法参数可解析但仍返回 2；
4. 非受支持 live profile 被 argparse 拒绝；
5. subprocess 直接运行脚本时无需 `.env.*`，无 stdout，exit 2；
6. 当前 decision main 保持无参同步签名，防止用修改核心入口“修复”legacy caller。

运行 focused、start/process-related、全量 unit 与 Ruff。

## 13. 迁移、回滚与兼容

保留文件路径和旧参数拼写，便于外部脚本得到可操作的确定性错误；不再保留错误的成功
期待。任何依赖旧 JSON summary 的自动化都会非零失败，必须迁移到当前 API/UI 或明确的
integration harness。

回滚到旧 TypeError 路径没有价值，不应作为生产方案。若未来确需支持有限迭代 CLI，
应围绕当前 runtime composition、profile 安全、依赖生命周期和证据输出新建设计。

## 14. 配置与环境隔离

旧脚本不再解析或加载 `.env.*`；profile 参数只为兼容诊断而解析，不产生有效 runtime
身份。受支持入口仍遵守当前 managed profile、loopback 与 live NO-GO 约束。

## 15. 代码组织与依赖

预计修改：

- `scripts/run_local.py`；
- 新增 `tests/unit/test_fs011_legacy_run_local_fail_closed.py`；
- 根 README、测试入口、code review、SOW 与全系统审计矩阵。

不新增第三方依赖，不修改 decision-engine public process entry。

## 16. 文档、运维手册与验收标准

本阶段验收：

- 旧入口不再抛签名 TypeError，也不在失败前加载 profile；
- 旧命令稳定给出迁移路径并非零退出；
- 文档不再只写“当前坏了”，而是准确写成“保留的迁移失败入口”；
- focused、related、full unit、Ruff、文档链接和 diff check 通过，或准确披露环境阻塞；
- FS-011 更新为代码关闭/外部调用方迁移开放；
- 真实资金生产继续 NO-GO。

最终关闭还需在 committed candidate 上由独立 reviewer 运行旧命令，并确认仓库外自动化
没有继续把该脚本当作成功路径；如果存在，必须完成调用方迁移后再标 CLOSED。

实施与验证结果见
[`37-fs-011-legacy-run-local-fail-closed.md`](../../audit/full_system_2026_08_24/37-fs-011-legacy-run-local-fail-closed.md)。
