# RDP 全链路业务逻辑审查报告

> 文档状态：现行审查报告（实现与本地验证完成；WSL2 集成环境阻断）
> 审查日期：2026-08-25
> 起始代码基线：`70f1a581`（分支 `main`）
> 变更状态：未提交工作树
> 任务书：[`../task/rdp_full_business_logic_audit_and_remediation_sow_2026_08_25.md`](../task/rdp_full_business_logic_audit_and_remediation_sow_2026_08_25.md)
> 运行边界：未执行 recommendation 审批、release、parameter apply/rollback、live profile 启动或真实资金操作

## 1. 结论

本轮确认并修复了 RDP 执行可行性计算、Phase 4 产物契约、完整流水线结果、任务重试、orphan recovery、调度 catch-up、Run/Attempt/Step/Event 状态单调性、事件查询、手工重试门禁、旧重试入口、组合发布授权和前端错误呈现中的业务逻辑缺陷。

用户截图中的失败并非“正常排队”：`directional/15m` 在无可用盘口数据时生成了空字符串数值，Phase 4 聚合器随后执行 `"" <= 0`，触发确定性的 `TypeError`。旧 daemon 又把所有失败无差别安排为 15 分钟后自动重试；完整流水线继续执行后续阶段，UI 只展示日志末尾，因而形成“前面失败、后面步骤成功、第二次尝试 pending”的误导状态。本轮已同时修复数据契约、失败摘要和重试决策，避免只压制表象。

静态与本地测试能证明上述代码契约，但不能证明策略盈利、交易所连通、容器健康、模拟盘新鲜度或任何 live trading readiness。当前 live profile 的硬门禁没有在本轮解除。

## 2. 审查范围与真源

审查以当前代码、配置、数据库迁移和测试为真源，覆盖：

- `configs/rdp_workflows/` 的 workflow、enabled、timeout、schedule 与任务顺序；
- `scripts/rdp_run_*`、Phase 2–6、归因、execution realism、决策与治理产物；
- task daemon、dispatcher、scheduler、retry manager、run observer；
- `governance.rdp_task_queue` 与 Run/Attempt/Step/Event 数据访问层；
- `/rdp`、`/rdp/v2` Operator API、鉴权、action token 和前端动作/状态展示；
- 相关 unit/integration 测试及现行 RDP/Operations 文档。

历史设计文档只用于定位原意；凡与当前实现冲突，均以本报告所列代码和现行文档为准。

## 3. 已确认问题与整改

| 编号 | 优先级 | 已确认问题 | 业务影响 | 整改结果 |
| --- | --- | --- | --- | --- |
| RDP-001 | P0 | Phase 4 CSV 空数值保持为 `""`，聚合时与整数比较 | 完整 RDP 确定性崩溃 | CSV 数值统一规范为有限浮点或 `None`；聚合器显式处理空值、NaN/Inf |
| RDP-002 | P0 | 所有 daemon 失败都自动重试 | 代码 bug、数据门禁和业务拒绝被无意义重复执行，队列长时间显示 pending | 失败分类为 transient/code/data-business/unknown；仅临时基础设施故障自动重试一次 |
| RDP-003 | P0 | `/releases/create` 和 `approve-and-release` 可应用参数但不要求 apply token | 组合入口绕过直接 apply 的第二道授权 | 所有实际 apply 路径统一要求 action-bound 短时 token；前端自动签发并携带；`skip_apply=true` 保持免 token |
| RDP-004 | P1 | 完整流水线没有机器可读的权威结果，后续成功日志掩盖首个失败 | UI 只能展示 `Process exited with code 1` 或错误日志尾部 | full pipeline/workflow 输出结构化结果标记，贯通首个失败阶段、错误摘要、失败类别和 warning/partial 状态 |
| RDP-005 | P1 | argparse/config 的 exit 2 被一概解释为“部分成功” | 配置错误可能被错误降级 | 只有明确支持 partial 语义的阶段把 exit 2 解释为 partial，决策/治理配置错误保持 failed |
| RDP-006 | P1 | worker 迟到结果可覆盖已被恢复或取消的 terminal task | 终态回退、重复重试、审计失真 | task terminal 更新要求当前仍为 running；daemon 忽略 terminal task 的迟到结果，不再入队重试 |
| RDP-007 | P1 | daemon 启动时恢复所有 running task | 并行 daemon 启动可误杀仍在执行的任务 | 仅原子领取心跳超过 30 秒的 stale running task，并周期扫描；新鲜心跳不受影响 |
| RDP-008 | P1 | Run/Step observer 可把终态重新写为 running 或其他终态 | Run/Attempt/Step 生命周期不单调 | Run terminal CAS、Step terminal monotonic upsert、active-only progress sync；未命中更新不再追加伪事件 |
| RDP-009 | P1 | 事件详情先取最早 N 条 | 长运行会隐藏 terminal/retry 等最新证据 | 内层按时间倒序取最近 N 条，外层再按时间正序展示 |
| RDP-010 | P1 | scheduler 为多个历史 slot 逐个入队，但任务命令只接受滚动窗口且 active 唯一 | 实际只执行第一条，仍误推进所有 slot 水位 | 漏执行 slot 合并为一次截至最新 slot 的任务；active 冲突不推进水位，下轮继续评估 |
| RDP-011 | P1 | manual/retry 辅助入口未统一检查 freeze/disabled | 可绕过 UI 的“不可运行”状态 | 手工触发、v2 retry 和 legacy retry 统一调用当前 availability/freeze 门禁 |
| RDP-012 | P1 | legacy retry manager 使用 `shell=True` 且有独立执行语义 | 命令安全、timeout、输出和错误分类漂移 | 复用 dispatcher 的 `shell=False` 执行和统一错误契约 |
| RDP-013 | P1 | 两个内容相同的 task dict 使用 `list.index` 计算剩余步骤 | 可能跳错后续任务 | 使用当前枚举索引确定剩余任务 |
| RDP-014 | P1 | 合法无数据与非法数值边界没有稳定语义 | NaN 污染、未处理异常或错误候选排名 | fill/slippage/cost 对数量、配置和有限值失败关闭；合法无市场数据保持 `no_data` |
| RDP-015 | P2 | Phase 4 slippage artifact 丢失 `feasibility_category` | 聚合器只能靠派生字段猜测 no-data | artifact 明确保留 feasibility 分类 |
| RDP-016 | P2 | UI 对 Run/Attempt/Step 状态和失败统一使用技术英文/危险色 | warning、partial 和 hard failure 无法区分 | 状态中文化；优先显示权威 error code/summary；warning/partial 使用对应提示级别 |
| RDP-017 | P1 | 旧 Round 重跑脚本用 `shell=True` 执行由 manifest 拼接的命令 | 被篡改的 artifact 可改变 shell 语义，且执行环境可能漂移 | retry plan 同时生成结构化 argv；当前解释器以 `shell=False` 执行，缺失/非法 argv 失败关闭 |
| RDP-018 | P2 | RDP CLI、指标描述、docstring 和注释残留 UTF-8 替换字符 | 用户提示与审计材料乱码，指标含义不完整 | 按代码上下文恢复中文，并对非产物源码执行全仓替换字符扫描 |

## 4. 修复后的关键业务语义

### 4.1 执行可行性与 Phase 4

- 无盘口/成交量数据是 `no_data`，不是 Python 异常；
- 数量、成本、edge 或模型参数为非有限值时失败关闭，不进入候选排名；
- artifact 中的空数值使用 `null`/`None` 语义，不使用空字符串冒充数字；
- 聚合只对有限数值计算成本和失败原因。

### 4.2 Run、Attempt、Step 与 Event

- Run/Task 一旦进入 terminal，不接受迟到 worker 或 observer 的状态覆盖；
- Step 一旦 terminal，后续 observer 只能保留已有终态；
- 详情接口返回最近 N 条事件并按时间正序排列；
- `succeeded_with_warnings` 与 `partially_succeeded` 不再被压扁成普通成功或硬失败；
- 首个失败阶段是结构化权威字段，日志 tail 仅作补充。

### 4.3 重试与恢复

- 自动重试只用于数据库暂不可达、依赖暂不可用、网络/超时等 `transient_infrastructure`；
- 自动重试最多一次，且沿用 logical run、创建下一 attempt；
- 确定性代码错误、数据质量/新鲜度门禁、业务 gate 与未知失败保持终态；
- orphan recovery 要求 running task 的 heartbeat 超过 30 秒未更新；
- manual retry 仍需重新通过 workflow 当前 enabled/freeze 门禁。

### 4.4 Scheduler

当前 workflow 命令使用滚动窗口或内部 watermark，并不接收 scheduler 历史 slot。多个漏执行 slot 因而合并为一次最新窗口执行；若队列已有同 workflow active task，则不推进 `last_processed_slot`。这避免了“调度记录显示全部补跑、实际只跑第一条”的假成功。

### 4.5 参数发布授权

下列路径只要会执行 apply，都要求当前 Operator session 签发的 `action=apply` 短时 token：

- `POST /rdp/parameters/apply`；
- `POST /rdp/releases/create`；
- `POST /rdp/recommendations/{id}/approve-and-release`。

组合端点在 approve、release 或 active parameter 写入之前校验 token。`skip_apply=true` 不执行参数应用，因此不要求 apply token。Rollback 使用独立 `action=rollback` token，不能复用 apply token。

## 5. 数据与兼容性

- 未新增或修改数据库 migration；
- HTTP 路径未改变；结构化结果和错误字段为向后兼容扩展；
- 组合 apply 端点新增安全必需的 header 契约，是有意的失败关闭行为变化；当前 UI 已同步；
- 既有 task/run 表状态枚举未增加；等待重试继续表示为 `queued + earliest_start_at + auto_retry`；
- active parameter 的 Postgres、history/release 和 runtime provenance 仍必须在真实部署后分别核对，任何单一文件或 HTTP 200 都不能证明已生效。

## 6. 验证证据

截至报告生成时已完成：

- 项目要求的 `ruff check aats/ --fix`：通过；全部改动 Python 文件 Ruff：通过；
- 全量 `tests/unit/`：`4647 passed, 30 skipped, 94 subtests passed`；
- 最终 RDP Operator token、production workflow API 与 Dashboard UI 集成集：`139 passed`；
- 最终受影响逻辑定向集：`113 passed`；安全 Round 重跑契约另行定向验证 `2 passed`；
- JavaScript 语法检查、`git diff --check`：通过；
- 15 份本轮变更/新增 Markdown 的本地相对链接检查：通过；
- 排除 artifacts/logs/data 后的源码 UTF-8 replacement-character 扫描：0 命中。

WSL2 可启动且 Docker Server `29.1.3` 可达，但规定路径 `~/aats-venv` 不存在，系统也未发现可用的 Linux Python；WSL 仓库仍在旧提交 `baffb50c`，而本轮实现是 Windows `70f1a581` 之上的未提交工作树。根据禁止手工同步、必须提交后走标准同步/部署入口的约束，本轮没有把未提交代码复制到 WSL，也没有用旧代码冒充集成验证。因此隔离 Postgres 并发测试标记为环境阻断，而非通过。

## 7. 剩余风险与非结论

1. 本轮未在 WSL2 Postgres 上制造真实 daemon 崩溃、并行 claim 与迟到 worker；数据库并发 SQL 仍需在代码提交并标准同步后执行最窄集成验证。
2. 本轮没有部署未提交工作树，因此现有本地服务若仍在运行，不包含这些修复。
3. action token 是 session-bound 的短时第二道授权，不是独立 MFA；它降低组合入口误调用和跨 actor 重放风险，但不能替代 session、RBAC、审批和 gate。
4. execution realism 仍是基于现有研究数据的模型，不是实盘 fill 证明；无数据被正确标记后，仍需补齐可靠 order-book/trade 数据覆盖。
5. 本报告不评价策略预期收益、统计显著性、容量、真实手续费或滑点外推。
6. live profiles 继续失败关闭；不得以本轮测试结果解除该门禁。

## 8. Operator 验收建议

代码提交并按标准流程部署到 derivatives 模拟栈后，只做无资金副作用的验收：

1. 查看 daemon、Gateway、Postgres 和 `/system/health`，区分服务存活与 RDP 数据新鲜度；
2. 触发允许的只读/研究 workflow，确认点击后立即创建 task/run，而不是等待 scheduler slot；
3. 人为提供一个确定性失败 fixture，确认不会自动出现第二次 pending attempt；
4. 提供一个 transient fixture，确认只产生一次延迟 retry，并保留同一 logical run；
5. 在 UI Run 详情核对首个失败阶段、中文状态和最近事件；
6. 不执行批准并发布、创建发布、apply、rollback 或任何 live profile 操作。
