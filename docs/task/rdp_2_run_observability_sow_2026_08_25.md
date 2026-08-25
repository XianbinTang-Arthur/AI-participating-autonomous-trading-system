# RDP 2.0 运行可观测性纵向切片实施任务书

> 文档状态：实现完成，待本地 derivatives 模拟栈部署验收
> 最后核对：2026-08-25（代码基线 `0b58dbad49735a89c1f02d6f69f74e8341ec8680`）
> 核对范围：当前 RDP 队列、daemon、workflow dispatcher、Operator API、Dashboard 快照与 RDP 前端
> 运行时边界：本文不证明当前研究结果有效、参数可发布或系统具备实盘交易资格

## 1. 业务目标与边界

本任务交付 RDP 2.0 的第一条可运行纵向切片：让操作员能够立即得到持久化运行标识，准确区分手工运行、调度运行与自动重试，查看排队原因、最早执行时间、当前步骤和终态结果，并让 RDP 页面不再把冷快照或延迟重试错误描述为 daemon/数据库故障。

本阶段不重写研究算法，不改变任何交易策略、资金预算或下单路径，不启用 profile apply/rollback，不放宽 Gate、Step2 integrity、holdout 或 live profile 门禁。本阶段也不删除 V1 API、现有 `rdp_task_queue` 或历史 workflow report。

## 2. 模块职责与领域模型

- `RdpRun`：一次逻辑运行，跨首次尝试和自动重试保持稳定 `run_id`。
- `RdpRunAttempt`：由现有 `rdp_task_queue` 行承担；每次 daemon 执行对应一次 attempt。
- `RdpRunStep`：workflow 内可观测步骤；本阶段覆盖 dispatcher 的顶层任务，后续扩展完整 Phase 2-6 子阶段。
- `RdpRunEvent`：运行生命周期追加事件；本阶段保留数据模型与写入接口，前端主要读取 Run/Attempt/Step 投影。
- V2 Run API：接收运行命令并提供一致性读模型；V1 task API 保持兼容。
- RDP 前端：显示真实运行、排队与重试状态，动作按资源隔离，不依赖全局整页等待文案。

## 3. 输入与输出接口

新增兼容前缀 `/rdp/v2`：

- `POST /rdp/v2/runs`：创建运行；返回 `202`、`run_id`、attempt 与队列信息。
- `GET /rdp/v2/runs`：分页/过滤查询逻辑运行。
- `GET /rdp/v2/runs/{run_id}`：查询 Run、attempts、steps 与结构化错误。
- `POST /rdp/v2/runs/{run_id}/cancel`：queued 立即取消；running 登记请求，daemon 先 terminate，10 秒未退出再 kill，并写 `cancelled` 终态。
- `POST /rdp/v2/runs/{run_id}/retry`：只允许终态失败运行创建下一 attempt。

所有写接口从认证 Principal 绑定 actor；不信任 request body actor。创建接口支持 `Idempotency-Key`，重复提交返回同一 Run，不重复入队。

## 4. 数据库 Schema、索引与约束

新增 ledgered Batch B migration：

- `governance.rdp_runs`
  - `run_id` 唯一；
  - `workflow`、`status`、`research_outcome`；
  - `trigger_kind`、`requested_by`、`idempotency_key`；
  - `eligible_at`、`started_at`、`finished_at`、`heartbeat_at`；
  - `current_step_key`、`completed_steps`、`total_steps`；
  - `cancel_requested_at`、`source_run_id`、`error_code`、`error_summary`；
  - `payload`、`created_at`、`updated_at`。
- `governance.rdp_run_steps`
  - `step_run_id` 唯一；
  - `(run_id, attempt_no, step_key)` 唯一；
  - 顺序、状态、allow_failure、时间、exit code、错误、日志/产物引用与 payload。
- `governance.rdp_run_events`
  - `(run_id, sequence_no)` 唯一；
  - event type、attempt、step、payload、时间。
- 扩展 `governance.rdp_task_queue`
  - `run_id`、`attempt_no`、`parent_task_id`、`trigger_kind`、`priority_class`、`heartbeat_at`、`cancel_requested_at`。

迁移需为历史 task 创建一一对应的兼容 Run，默认 `run_id=task_id`，并提供逆向 rollback SQL。不得修改已部署 migration 内容或 checksum。

## 5. 事务、一致性与并发

- 创建 Run 与首个 queue attempt 必须在同一数据库事务中完成。
- `Idempotency-Key` 由数据库唯一约束兜底。
- 现有同 workflow pending/running 唯一约束继续生效；V2 将冲突映射为结构化 `409`。
- claim 使用现有 `FOR UPDATE SKIP LOCKED`，不改变 worker 安全语义。
- Run 聚合状态由 attempt/step 写入函数同步更新；终态更新必须和 attempt 终态处于同一事务。
- cancel 使用队列行→Run 的一致锁顺序：queued 原子取消，running 进入 `cancellation_requested`；终态请求返回幂等结果。
- retry 仅允许 failed/partially_succeeded 且无 active attempt 的 Run。

## 6. 认证、授权与数据安全

- GET 使用 `require_read_access`。
- 创建、取消、重试使用 `require_write_access`。
- actor 绑定 session/API-key Principal；认证开启时禁止 body 覆盖。
- API、日志和前端不返回 DSN、环境变量、token、session cookie 或未经清理的完整 traceback。
- 本阶段不增加任何 live-funds 操作权限。

## 7. 错误处理与幂等

- 使用 V2 结构化错误：FastAPI `detail` 内返回稳定 `code`，并按场景补充
  `message`、`retryable`、当前 `status` 或活动任务；本阶段不虚构尚未接入的
  全局 `correlation_id`。
- 数据库不可达返回 `503`；等价活跃任务或状态冲突返回 `409`；非法状态返回 `422`。
- daemon orphan recovery 继续使用 exit code `-3`，并同步 Run 错误码 `worker_orphan_recovered`。
- 自动重试必须复用原 `run_id`，增加 `attempt_no`，不得再伪装成独立运行。

## 8. 状态迁移与生命周期

Run 执行状态：

`queued -> running -> succeeded | succeeded_with_warnings | partially_succeeded | failed | cancelled`

辅助状态：

- queued/running 可进入 `cancellation_requested`；
- failed 可创建新的 attempt 并返回 queued；
- `research_outcome` 与执行状态独立，默认 `unknown`。

Step 状态：

`pending -> running -> succeeded | failed | skipped | cancelled`

## 9. 缓存与性能

- V2 Run API 直接读取小型 governance 索引，不走六个 Dashboard snapshot panel。
- 列表默认 20 条、最大 100 条；详情 steps/events 分开限制。
- Run 列表索引覆盖 status、workflow、created_at；Step 索引覆盖 run/attempt/order。
- 前端只定向刷新运行数据；不因单个动作刷新全部 Dashboard。
- 本阶段不引入新缓存中间件；Postgres 是权威真源。

## 10. 日志、监控与审计

- 创建、claim、step 开始/结束、终态、取消请求、重试均写结构化事件。
- daemon heartbeat 同步到 active Run/attempt。
- 日志只记录 ID、状态、错误码和非秘密摘要。
- 后续 Prometheus 指标以 Run/Event 为来源；本阶段至少保留可聚合字段。

## 11. 测试策略

- migration SQL、注册顺序、rollback 与 ORM 契约单元测试；
- Run repository 创建、幂等、claim、终态、重试、取消、历史兼容测试；
- V2 API 认证、202/409/422/503 与 actor 绑定测试；
- daemon 自动重试复用 run_id 测试；
- 前端渲染：queued retry、running step、partial/failed、合法空状态；
- 最窄 WSL2 PostgreSQL 集成测试验证 migration 与事务并发。

## 12. 迁移、回滚与兼容

- V1 `/rdp/tasks/*` 不删除、不改响应字段；允许追加字段。
- daemon 同时维护新 Run 与旧 task 状态。
- 迁移先创建新表和列，再回填历史数据，再添加非空/唯一约束。
- rollback 删除新增 FK/索引/列/表，不删除历史 `rdp_task_queue` 行；V2 新增的
  `cancelled` 终态会在恢复旧约束前保守降级为 `failed` 并保留错误摘要。
- 前端先消费 V2；V1 控制卡保留到后续阶段完成。

## 13. 配置与环境隔离

- 不新增 `.env` 配置，不读取任何凭证文件。
- 默认队列与 timeout 保持当前 profile 行为。
- live profile 部署门禁不变；只在本地 derivatives 模拟栈做运行验证。

## 14. 代码组织与依赖

- `aats/api/rdp_v2.py`：Run V2 router 与请求 schema。
- `aats/data_platform/governance/rdp_runs_db.py`：Run/Step/Event repository。
- 现有 `rdp_task_db.py` 负责兼容 queue attempt，并调用/协同 Run repository。
- 前端在现有模块体系中新增 `rdpRuns` snapshot panel、运行中心和详情抽屉；
  保留旧 control/workbench 作为兼容投影。
- 不引入新的第三方前端框架或 Python 依赖。

## 15. 文档与运维手册

- 更新 `docs/rdp/README.md`、`docs/rdp/module_reference.md`、`docs/operations/rdp_operator_workflow.md`。
- 文档明确 V1/V2 并行、协作式取消、自动重试与运行时未知边界。
- 不把本地测试结果写成永久运行结论。

## 16. 部署与验收标准

部署只允许使用标准命令 `bash scripts/deploy.sh --profile derivatives --skip-commit`，不得手工 Compose。

验收要求：

1. 点击运行立即获得稳定 `run_id`；
2. 自动重试保持同一 `run_id`、attempt 递增并显示 `eligible_at`；
3. Run 详情展示真实 queue、当前顶层 step、错误与终态；
4. 页面不再把 snapshot loading 猜测为 daemon/DB 故障；
5. V1 API 与现有 daemon workflow 不回归；
6. Ruff、完整 unit、最窄 WSL2 集成和本地浏览器验证通过；
7. active parameters、Gate、release、apply、rollback 和主交易行为无变化。

### 当前验证证据（2026-08-25）

- `.venv\Scripts\python.exe -m ruff check aats/ --fix`：通过；
- 完整 `tests/unit/`：`4608 passed, 30 skipped, 94 subtests passed`；
- RDP 前端布局/交互集成：`tests/integration/test_dashboard_ui.py`，`99 passed`；
- WSL2 隔离 PostgreSQL：Stage 1-17 migration/rollback 与 Run 生命周期测试，
  `2 passed`；
- JavaScript 语法检查：`app.js`、`rdp-actions.js`、`rdp-view.js`、
  `rdp-control-panel.js` 全部通过；
- 尚未把未提交工作区部署到本地 derivatives 模拟栈，因此本文不声称浏览器页面
  或当前运行容器已经加载本次代码，也不声称任何研究/收益结论。

## 17. 后续阶段

本任务完成后再依次交付：数据质量矩阵、完整 Phase 2-6 子步骤事件、候选证据对比、结构化审批与 Release Saga、观察/运维页面，最后退役旧六面板聚合工作台。
