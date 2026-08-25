# AATS 上线前本地测试指南

> 文档状态：现行操作说明  
> 最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`，包含 Phase 3A–3W 整改提交候选）
> 核对范围：测试目录、仓库命令、managed profile、本地 API 入口和部署纪律的静态核对  
> 运行时状态：未验证；本文不证明数据库、容器、交易所或实盘链路可用

本文给出“本地静态验证 → 单元/场景测试 → WSL2 集成 → 模拟运行 → 上线审批”的分层入口。每一层的通过只证明该层范围，不能替代下一层，更不能等价为实盘放行。

收益证据专项的可执行验收与 NO-GO 传播规则见 [`profit_readiness_acceptance.md`](profit_readiness_acceptance.md)。

## 1. 强制安全边界

- 本地测试默认使用 `derivatives` 模拟 profile；禁止为了“更真实”改用 `derivatives_live`。
- 不读取、打印、提交或复制 `.env.*` 的内容。只检查文件是否按操作规范存在，不展示值。
- 不用真实资金、真实下单或绕过 risk、kill switch、reconciliation、trading-ready 等硬门。
- 不手工执行 `docker compose`，不使用 `rsync`；需要部署式演练时只走 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md) 规定的入口。
- `scripts/run_local.py` 是只输出迁移指引并 exit `2` 的失败关闭入口，不能作为 paper loop；有限迭代闭环使用明确选择的 integration scenario。
- 测试失败、skip 原因不明、运行时证据缺失或文档与代码冲突时停止推进，不以人工“看起来正常”覆盖失败。
- 回测验证必须同时记录 `next_bar_event_v2` 与 `ohlcv_participation_cap_v2`；OHLCV participation-cap 通过不能替代 L2/历史真实 fill 校准，也不能作为 live 容量或收益证明。
- Dashboard 无障碍单元测试只锁定 modal/focus/reduced-motion 代码契约；上线前仍须在目标浏览器完成 keyboard-only、NVDA/VoiceOver、axe、缩放和 reduced-motion 人工验证。
- Managed profile 测试必须证明 YAML 是 mapping 且零未知 `AATSSettings` key；静态/单元通过仍不能替代 committed candidate 的目标进程启动与仓库外 overlay 盘点。
- 面向某个 profile 的 independent replay 必须把解析后的 `strategy_short_bias_enabled`
  作为显式参数写入 artifact；字段缺失只能按兼容默认解释，不能据此声称验证了 long-only
  配置。关闭值必须使 short raw score 为 `0.0`。

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

仓库还定义了 `.github/workflows/quality.yml` 基础质量门：在 pull request、`main` push
或人工触发时使用 Python 3.12 执行全仓 Ruff、完整 unit、strict markers 和新增 warning
阻断。workflow 只读 checkout、不读取 secrets、不部署。当前唯一 warning allowlist 是
精确匹配的 SQLite 默认 datetime adapter 弃用消息；不得扩大为整类忽略。

Phase 3T 起，workflow 会先运行 `scripts/verify_dependency_locks.py`，再从
`requirements/ci-py312-linux-x86_64.lock` 按 `--require-hashes --only-binary=:all:`
安装。运行时 Docker 使用对应 runtime lock，外部 Compose image 使用 tag + digest。

Phase 3U 又在安装第三方依赖前运行
`scripts/verify_database_connection_budget.py`。该检查扫描应用 `create_engine` inventory、
pool 单一真源、声明 topology 150、Compose 普通容量 197/名义余量 47 和 workflow 接入。
它不连接数据库，也不能替代 WSL2 全拓扑负载、慢查询、故障重连、恢复/admin 竞争和联合
内存测试。FS-008 详细边界见
[`../task/fs_008_database_connection_budget_sow_2026_08_25.md`](../task/fs_008_database_connection_budget_sow_2026_08_25.md)。

Phase 3V 的 FS-004 单元契约只证明 real-data v2 中 evaluator 收到 train/valid rows、双门
失败关闭、test 内容变化只改变 seal，以及 candidate/recommendation lineage 闭合。它不
访问历史 artifact 或 test 数据，不是最终 OOS 运行。上线前若任何策略依赖 Research
Factory 证据，还必须执行只读历史 lineage 审计、独立一次性 holdout 评估、walk-forward/
multiple-testing 复核和 production gate 验证；详见
[`../task/fs_004_research_selection_holdout_sow_2026_08_25.md`](../task/fs_004_research_selection_holdout_sow_2026_08_25.md)。

该文件存在不证明 GitHub 远端已经成功运行或 ruleset 已把它设为 required check；本地
验证也不能替代远端日志。当前门禁尚不覆盖 integration、Node/browser、Compose/schema
运行、APT、clean Docker build、SBOM、secret/CVE/license/provenance，详见
[`../task/fs_021_ci_quality_gate_sow_2026_08_25.md`](../task/fs_021_ci_quality_gate_sow_2026_08_25.md)、
[`../task/fs_022_reproducible_dependencies_sow_2026_08_25.md`](../task/fs_022_reproducible_dependencies_sow_2026_08_25.md)
与 [`../../audit/full_system_2026_08_24/40-fs-022-reproducible-dependencies.md`](../../audit/full_system_2026_08_24/40-fs-022-reproducible-dependencies.md)。

当前 Windows `.venv` 可能使用 Python 3.14，而 workflow/生产镜像目标为 3.12。3.14
会额外报告未关闭 SQLite connection 的 `ResourceWarning`；这是真实兼容性信号，不能
冒充 3.12 CI 结果，也不能据此给 workflow 增加宽泛豁免。

当前 Windows 主机可能因用户系统临时目录 ACL 使原样 pytest 命令在 `tmp_path`
fixture setup 报 `PermissionError`。必须先保留原样命令的失败证据，再使用仓库内全新、
本次运行唯一的 `--basetemp` 目录复跑；不得复用旧目录或把环境错误记成测试通过。
这一替代只用于区分临时目录权限与断言失败，不改变测试范围。

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
启动器会固定 `AATS_PROCESS_ROLE=monolith`，因此这里验证的是完整单进程 runtime，而不是缺少
market/decision/execution slice 的孤立 Gateway。

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

当前所有 live profile 都在部署副作用前硬禁用，不能执行 live 验证。future
`derivatives-live` required list 已包含 liquidation 与 microstructure 两个采集器，但该声明
尚未在目标 Compose 环境验证；即使未来部署脚本返回成功，也仍须按 trading-readiness
packet 核对组件 health、数据 freshness 与故障告警。

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
