# RDP 全链路业务逻辑审查与异常修复任务书

> 文档状态：实现与本地验证完成；WSL2 集成环境阻断
> 最后核对：2026-08-25（起始代码基线 `70f1a581`）
> 核对范围：RDP 前端、Operator API、Run/Attempt/Step/Event、任务队列、调度器、daemon、workflow dispatcher、研究 Phase 2–6、治理与产物持久化、相关配置和测试
> 运行时边界：本任务不授权 recommendation 审批、参数 apply/rollback、live profile 启动或任何真实资金操作

## 1. 业务目标与边界

本任务的目标是让 RDP 在研究、运行和 Operator 展示三个层面保持一致、可解释、可恢复和失败关闭：

- 合法的数据缺失、部分成功和研究门禁不应被误报为程序异常；
- 程序异常必须保留首个失败阶段、结构化错误和可操作恢复建议；
- Run、Attempt、Step 与 Event 的状态必须满足单调、可审计的生命周期；
- 自动重试只处理具备恢复可能的临时故障，不重复执行确定性代码或业务门禁失败；
- UI 必须展示后端权威状态，不能用日志末尾掩盖首个失败原因；
- 所有发布或运行参数变更路径继续失败关闭，不在本任务中恢复写能力。

本轮不评价策略是否盈利，不把 replay、bar-proxy execution realism 或研究报告外推为实盘成交能力，也不以 HTTP 200、容器健康或单元测试代替 trading-ready 结论。

## 2. 模块职责与领域模型

审查范围按下列责任边界组织：

1. `configs/rdp_workflows/`：workflow 声明、任务顺序、超时、失败策略与调度频率。
2. `workflow_scheduler.py`：UTC slot、bootstrap、catch-up、幂等入队与调度状态。
3. `rdp_task_db.py` / `rdp_runs_db.py`：Run、Attempt、Step、Event 的数据库真相与状态转换。
4. `rdp_task_daemon.py`：claim、心跳、取消、子进程生命周期、重试与 orphan recovery。
5. `workflow_dispatcher.py`：任务执行、输出捕获、失败分类和 workflow 报告。
6. `scripts/rdp_run_*` 与 `aats/data_platform/*`：研究、归因、执行可行性、治理和决策产物。
7. `rdp_v2.py` / `rdp_routes.py` / `rdp_control_summary.py`：鉴权、校验、查询和 Operator 命令面。
8. RDP 前端模块：状态映射、操作反馈、轮询、详情抽屉、错误与空状态。

领域状态以数据库枚举和当前代码为准。任何兼容端点都不得绕开 v2 Run/Attempt 状态机。

## 3. 输入、输出与接口

- 输入包括受 allowlist 约束的 workflow 名称、Operator session、数据库队列状态、研究数据和已生成产物。
- 输出包括 Run/Attempt/Step/Event 记录、workflow report、Phase 产物、治理建议与只读 UI 展示。
- 公共 HTTP 方法、路径和响应兼容性默认保持；若需要新增结构化字段，只允许向后兼容扩展。
- 错误响应不得包含凭证、连接串、session cookie、token 或未经裁剪的敏感环境信息。

## 4. 数据库表、索引与约束

重点复核 `governance.rdp_task_queue`、`rdp_runs`、`rdp_run_steps`、`rdp_run_events`、`rdp_runtime_status`、scheduler operational state 及研究/决策 snapshot 表：

- 同一 workflow 的 pending/running 唯一性；
- attempt number 与同一 run 的关系；
- claim 使用行锁且不会重复执行；
- terminal 状态不可回退；
- cancelled、failed、done 的时间戳和退出码一致；
- 查询所需索引覆盖列表、详情、事件和调度热路径。

只有确认现有 schema 无法表达正确约束时才新增迁移；迁移必须同时提供回滚脚本和测试。

## 5. 事务、一致性与并发

- Run 创建、Attempt 入队和初始 Event 必须在可解释的事务边界内完成。
- claim、heartbeat、cancel request、terminal finalize 与 retry enqueue 必须防止竞争覆盖。
- daemon 串行执行槽不能导致手工任务被无限期饿死；优先级与 eligible time 必须一致。
- artifact 文件与 DB snapshot 不能互相冒充成功；写 DB 失败时要保留明确降级状态。
- 任何重试都复用 logical run，递增 attempt，并防止递归重试。

## 6. 授权、认证与数据安全

- 所有 RDP 读取遵循 read access，写操作遵循 write access 与现行 action token/失败关闭约束。
- request body 中的 actor 不得覆盖认证主体。
- 路径、命令和 artifact 读取必须防止目录穿越及任意命令执行。
- 不读取、记录或展示 `.env.*`、数据库密码、API 密钥、cookie 或 token。
- 本任务不解除 profile apply/rollback 的 `501` 无写入边界。

## 7. 错误处理与幂等

错误至少区分：

- `transient_infrastructure`：临时网络、数据库连接、依赖不可用、可恢复超时；
- `deterministic_code_or_contract`：未处理异常、类型错误、契约破坏；
- `business_or_data_block`：数据不足、完整性/新鲜度门禁，或研究、治理、发布门未通过；
- `cancelled` 与 `worker_orphan_recovered`。

只有临时故障允许自动重试。合法空值、NaN/Inf、空集合、部分成功和无匹配数据必须有确定语义，不能触发未处理异常。

## 8. 状态转换与生命周期

目标生命周期：

```text
Run queued -> running -> succeeded | succeeded_with_warnings |
                         partially_succeeded | failed | cancelled
terminal retryable run -> queued (eligible_at 表示等待门) -> running (next attempt)

Attempt pending -> running -> done | failed | cancelled
Step pending -> running -> succeeded | failed | skipped | cancelled
```

Run 汇总必须由 attempts/steps 权威维护，不能长期停留在 `queued`；现行 schema 不新增 `retry_wait` 枚举，而以 `status=queued + eligible_at + trigger_kind=auto_retry` 表示等待门。重试耗尽后必须进入终态，后续阶段成功不能覆盖首个失败阶段。

## 9. 缓存与性能

- 列表和详情查询必须有界；日志、事件和 artifact tail 必须限制大小。
- scheduler catch-up、队列扫描、artifact index 和研究批处理不得产生无界内存增长。
- 前端轮询需要避免并发叠加、重复请求和隐藏页面持续高频刷新。
- 本轮不以性能重构替代正确性修复，发现容量风险时记录并提供可测量门槛。

## 10. 日志、监控与审计

- 记录 logical run、attempt、workflow、step、error class、首个失败阶段和 retry decision。
- 原始 tail 只作为诊断补充；UI 与 API 优先返回结构化摘要。
- 重试跳过、耗尽、取消、orphan recovery 和 snapshot 降级必须产生审计事件。
- 日志不得泄露敏感配置或完整未经裁剪的外部响应。

## 11. 测试策略

至少覆盖：

- 空字符串、`None`、NaN/Inf、无 bar 匹配和部分成功的 Phase 4 汇总；
- 首个失败阶段在 dispatcher、DB、API 和 UI 的贯通；
- 确定性错误不重试、临时错误只重试一次、retry wait 状态正确；
- claim/cancel/finalize 竞争与 terminal 状态单调性；
- workflow allowlist、disabled/frozen workflow 和命令安全；
- API 鉴权、状态码、响应兼容与前端中文展示；
- 现有 RDP 单元测试、全量单元测试和最窄 WSL2 集成测试。

## 12. 迁移、回滚与兼容性

- 优先采用无需迁移的向后兼容修复。
- 新增响应字段不删除旧字段；旧客户端继续可用。
- 若新增数据库字段或约束，提供正向/回滚 migration、旧数据回填和混合版本行为说明。
- 回滚代码后不得留下只能由新版本解释的 active queue 状态。

## 13. 配置与环境隔离

- workflow 名称、enabled、schedule、timeout 和 enqueue block 必须在配置、allowlist、daemon 与文档间一致。
- Windows 静态/单元环境和 WSL2 集成/模拟运行环境分开验证。
- live profile 继续在任何副作用前失败，不使用运行环境凭证进行审查。

## 14. 代码组织与依赖

- 业务状态和错误分类放入现有治理/operations 层，不复制到多个 route 或 UI。
- 避免为了单个异常引入新的框架或后台服务。
- 公共解析、数值归一化与状态聚合逻辑使用单一实现并由窄测试保护。

## 15. 文档与运维手册

修复完成后同步：

- `docs/rdp/` 当前模块说明；
- `docs/operations/rdp_operator_workflow.md`；
- 调度、重试或错误语义发生变化时更新相应 runbook/calendar；
- 纠正仍宣称可直接修改实盘参数的过时 UI 文案，保留历史材料的历史边界。

## 16. 部署与验收标准

代码验收：

1. Ruff 通过；
2. 新增和受影响 RDP 测试通过；
3. `tests/unit/` 全量通过；
4. 最窄 WSL2 集成测试通过，或明确报告外部环境阻断；
5. Git diff 复审无高优先级问题。

运行验收仅限 derivatives 模拟栈：

- daemon、Gateway 与依赖健康；
- 只读 API 能看到正确终态和首个失败原因；
- 不触发 recommendation 审批、release、apply、rollback 或 live 交易；
- 若需要重新部署，必须使用标准部署入口，且部署前代码已提交。

## 17. 实施结果（2026-08-25）

本轮在不新增数据库迁移、不改变 HTTP 路径的前提下完成：

- Phase 4 CSV 空字段统一为 `None`，聚合器仅比较有限数值，并把 `feasibility_category` 保留到 slippage artifact；
- fill/slippage/cost 对无效数量、NaN/Inf 和非法模型参数失败关闭，合法无市场数据保持 `no_data`；
- full pipeline 与 workflow wrapper 输出结构化结果标记，Run 保留首个失败阶段，并区分 warning、partial 与 failed；
- 自动重试仅接受 `transient_infrastructure`，确定性代码、数据/gate 和未知失败不再延迟 15 分钟重复运行；
- scheduler 将多个漏掉的滚动 slot 合并为一次最新窗口执行，active 冲突不再误推进水位；
- task/run/step 终态改为单调，迟到 worker 结果不再覆盖 orphan recovery 或触发后续 retry；
- orphan recovery 只原子领取超过 30 秒无心跳的 running task，避免并行 daemon 启动误杀；
- Run 详情事件改为“最近 N 条后按时间正序”，manual retry 重新检查当前 freeze/disabled 条件；
- legacy retry manager 复用 dispatcher 的 `shell=False` 执行和 freeze/disabled 门禁；
- 失败 Round 重跑计划新增结构化 argv，使用当前 Python 解释器和 `shell=False`，保留旧字符串字段仅作展示兼容；
- 所有实际 apply 路径统一要求 action-bound 短时 token，前端组合发布动作自动签发并携带；
- 前端 Run 详情使用中文状态，并按 warning/partial/failed 展示权威摘要；
- 清理 RDP 源码、CLI 提示、指标描述和相关测试/历史备份中的 UTF-8 替换字符。

详细证据见 [`../review/rdp_full_business_logic_audit_2026_08_25.md`](../review/rdp_full_business_logic_audit_2026_08_25.md)。

## 18. 验收结果

- `ruff check aats/ --fix` 与全部改动 Python 文件 Ruff：通过；
- 全量单元测试：`4647 passed, 30 skipped, 94 subtests passed`；
- 最终 RDP API/UI 集成集：`139 passed`；
- JavaScript 语法、`git diff --check`、15 份变更 Markdown 本地链接、UTF-8 replacement-character 扫描：通过；
- WSL2 Docker 可达，但 `~/aats-venv` 缺失且 WSL 仓库仍为旧提交 `baffb50c`。未提交工作树不能通过标准同步入口进入 WSL，因此 Postgres 最窄集成验证按环境阻断记录，未声称通过；
- 未部署、未启动 live profile、未执行审批、release、apply、rollback 或真实资金操作。
