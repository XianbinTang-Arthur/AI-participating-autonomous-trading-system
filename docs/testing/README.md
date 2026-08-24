# AATS 上线前本地测试指南

> 文档状态：现行操作说明  
> 最后核对：2026-08-23（代码基线 `be9179ead5be6aba22fbe94e3baf72b9f46eedc3`）  
> 核对范围：测试目录、仓库命令、managed profile、本地 API 入口和部署纪律的静态核对  
> 运行时状态：未验证；本文不证明数据库、容器、交易所或实盘链路可用

本文给出“本地静态验证 → 单元/场景测试 → WSL2 集成 → 模拟运行 → 上线审批”的分层入口。每一层的通过只证明该层范围，不能替代下一层，更不能等价为实盘放行。

## 1. 强制安全边界

- 本地测试默认使用 `derivatives` 模拟 profile；禁止为了“更真实”改用 `derivatives_live`。
- 不读取、打印、提交或复制 `.env.*` 的内容。只检查文件是否按操作规范存在，不展示值。
- 不用真实资金、真实下单或绕过 risk、kill switch、reconciliation、trading-ready 等硬门。
- 不手工执行 `docker compose`，不使用 `rsync`；需要部署式演练时只走 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md) 规定的入口。
- `scripts/run_local.py` 当前接口已经漂移，不能作为可信 paper loop。
- 测试失败、skip 原因不明、运行时证据缺失或文档与代码冲突时停止推进，不以人工“看起来正常”覆盖失败。

## 2. 证据分层

| 层级 | 证明内容 | 不能证明 |
| --- | --- | --- |
| L0 静态基线 | 代码版本、工作区差异、解释器和文档入口明确 | 程序可运行 |
| L1 lint/单元 | 局部语法、风格和隔离行为契约 | 跨进程、真实存储和外部依赖 |
| L2 场景/smoke | 内存态多组件闭环和业务场景 | NATS/Redis/Postgres 容器与网络 |
| L3 集成 | 在 WSL2/隔离容器中的真实依赖交互 | 当前生产实例与账户状态 |
| L4 模拟运行 | 本地 API、页面和模拟 profile 的运行行为 | live profile、真实下单、资金安全 |
| L5 上线前现场门 | 当时的健康、对账、风险、权限和恢复证据 | 未来持续健康 |

## 3. L0：记录基线

在仓库根目录的 Windows PowerShell 中执行：

```powershell
git rev-parse HEAD
git status --short
.\.venv\Scripts\python.exe --version
```

记录完整 commit、未提交文件和 Python 版本。脏工作区不是自动失败，但必须能解释每一项差异；上线候选应对应可复现、已审阅的 committed state。

然后阅读：

1. [`../../CLAUDE.md`](../../CLAUDE.md) 与 [`../../AGENTS.md`](../../AGENTS.md)；
2. [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)；
3. [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)；
4. [`../operations/operator_checklist.md`](../operations/operator_checklist.md)；
5. [`../code_review/README.md`](../code_review/README.md) 的已知限制。

## 4. L1：lint 与单元测试

仓库规定的最低命令：

```powershell
.\.venv\Scripts\python.exe -m ruff check aats/ --fix
.\.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
```

注意：`--fix` 可能修改文件，运行前后都要查看 `git status --short` 和 diff。若只想先诊断而不改文件，可先运行不带 `--fix` 的同一 lint 命令，但最终交付仍按 `AGENTS.md` 的要求执行。

针对单点变更可以先跑最窄测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/path/to/test_file.py -x -q
.\.venv\Scripts\python.exe -m pytest tests/unit/ -k "test_name" -x -q
```

最窄测试通过后仍要执行完整 `tests/unit/`。记录 passed、failed、skipped、warnings 和耗时；skipped 不得计作已覆盖。

## 5. L2：场景与内存 smoke

这两组不拉真实 Docker、不连接真实 Postgres/NATS，适合在单元测试后验证业务流程：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/scenario/ -x -q
.\.venv\Scripts\python.exe -m pytest tests/smoke/test_4proc_pipeline.py -x -q
```

WSL2 中另有 legacy CLI 禁用检查：

```bash
bash tests/smoke/test_rdp_legacy_scripts_disabled.sh
```

该 shell 检查只证明被禁用的旧 RDP 脚本仍保持阻断，不证明 RDP 当前工作流可运行。

## 6. L3：WSL2 集成测试

按项目操作手册，集成测试在 WSL2 中运行：

```powershell
wsl -d Ubuntu bash -c "cd ~/aats && source ~/aats-venv/bin/activate && pytest tests/integration/ -x -q"
```

集成目录包含不同前置条件：有些用仓库 fixture，有些需要 Docker/testcontainers，有些只有显式设置 `AATS_RUN_NATS_INTEGRATION=1`、`AATS_RUN_REDIS_INTEGRATION=1` 或 `AATS_RUN_POSTGRES_INTEGRATION=1` 才会运行。不要一次性打开所有开关；根据变更范围选择最窄文件，确认使用隔离容器和测试数据后再启用。

对每个集成测试记录：

- 文件与用例；
- WSL2、Python 和依赖版本；
- 是否启用外部依赖开关；
- passed/failed/skipped；
- 创建和清理了哪些隔离资源；
- 是否存在无法验证的生产差异。

## 7. L4：模拟 profile 本地运行

仅在 L1-L3 达到预期后启动本地 API：

```powershell
.\.venv\Scripts\python.exe scripts\start_api.py --profile derivatives
```

仓库模板中 `derivatives` 默认端口为 `8001`；以启动器实际打印 URL 为准。本地入口是 HTTP。不要把它写成标准 live TLS 地址，也不要将服务启动等价为 trading-ready。

最小只读验证建议：

1. 进程能启动并在预期 host/port 监听；
2. `/healthz` 能返回，但结果只表示 Gateway 存活；
3. `/openapi.json` 与预期路由一致；
4. UI 静态资源能加载，控制台无明显错误；
5. 只读 dashboard 能区分 stale、unavailable、not configured 与 ready；
6. 不触发 apply、rollback、release、operator recovery 或任何下单动作。

停止本地进程后确认端口释放、没有遗留子进程，并保存日志摘要。模拟运行通过仍不能证明 live 数据源、凭证、Postgres、Redis、NATS、TLS 或交易所账户正确。

## 8. L5：上线前现场门

真正部署或实盘放行前，回到 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md) 和 [`../operations/operator_checklist.md`](../operations/operator_checklist.md)。至少需要在当时重新验证：

- 目标 commit、profile 与环境身份；
- 所有应用和基础设施容器，而不只主四切片；
- TLS、认证和 Operator 权限；
- Postgres migration、Redis/NATS 连接和数据新鲜度；
- 交易所账户模式、余额、仓位和订单；
- kill switch、reconciliation、trading-ready 与恢复路径；
- active parameter version、审批和回滚证据；
- 告警、日志、指标、备份与安全停机。

当前 `scripts/deploy.sh` 的自动健康门未覆盖 derivatives-live 的两个采集器，因此不能只凭 deploy 脚本返回成功判断全栈健康。该限制修复并经测试前，必须把采集器单独验证列为人工检查项。

## 9. 测试记录模板

```text
候选 commit：
测试时间与时区：
执行人/复核人：
测试层级：L0 / L1 / L2 / L3 / L4 / L5
环境：Windows / WSL2 / 隔离容器 / 目标环境
命令或受控操作：
结果：passed / failed / skipped / blocked
关键输出摘要：
运行时验证：已验证项
未知项：
创建/清理资源：
是否允许进入下一层：否（默认）/ 是（附审批依据）
```

失败必须保留原始证据并分析原因；不得通过删除测试、放宽安全断言、切换 live profile 或跳过失败步骤来获得“通过”。
