# RDP Platform V3 重构实施任务书

> 文档状态：实现与本地定向验证完成，待发布/现场验收
> 日期：2026-08-25
> 起始 HEAD：`70f1a581a81f55697c9b68167539ad0db86fc06a`
> 设计真源：[`../design/rdp_platform_v3_architecture_2026_08_25.md`](../design/rdp_platform_v3_architecture_2026_08_25.md)
> 安全边界：仅发布和验收 `derivatives` 模拟环境；不解除 live profile、release cycle 或参数门禁

## 1. 业务目标与边界

将 RDP Operator 体验收敛为单一研究运营工作台，使研究阶段、执行队列、证据、审批、发布和观察可以在同一时间点被理解和操作。范围限于 RDP 控制面、Operator 前端、兼容 API、文档、测试与模拟发布；不替换研究数学引擎或主交易引擎。

## 2. 模块职责与领域模型

- `RdpWorkspaceAssembler`：组装同一时间点的 workspace 读模型；
- lifecycle projector：将数据、研究、治理、发布、观察状态映射到稳定阶段；
- execution-lane projector：计算 active run、队列位次、等待原因和执行槽容量；
- workflow catalog：从 JSON 和当前 freeze/enabled 门禁生成 capability；
- eligibility projector：仅标识已经具备发布资格的候选，不发明新收益分数；
- V3 UI：只渲染 workspace 合同，操作继续调用受保护的现有写 API。

## 3. 输入/输出接口

- 新增 `GET /rdp/v3/workspace` 只读接口；
- 输入只包含受限的 Run 列表数量，不接收文件路径或查询语句；
- 输出为 `rdp.workspace.v3` 版本化 JSON；
- 现有 `/rdp/v2/runs`、recommendation、gate、release、apply、rollback 写入合同保持不变。

## 4. 数据库 Schema / 表 / 索引 / 约束

不新增表或迁移。继续使用 `governance.rdp_runs`、`rdp_run_attempts`、`rdp_run_steps`、`rdp_run_events`、`rdp_task_queue`、recommendation/release/active parameter 表及已有 partial unique/CAS 约束。

## 5. 事务、一致性与并发

- workspace 是只读投影，不在读取过程写入任何业务表；
- Run 创建仍使用数据库原子入队、幂等 key 和 active workflow 唯一约束；
- workspace 组装记录单一 `generated_at`，子源 stale/error 必须显式返回；
- 本次不增加 daemon 并发度，避免 artifact/checkpoint 竞争。

## 6. 授权、认证与数据安全

- workspace 要求 `require_read_access`；
- 前端写操作继续由 session principal 绑定 actor；
- apply/rollback 继续要求 action-bound HMAC token；
- 不在返回体、日志、文档或测试 fixture 中包含凭证、cookie、token 或连接串。

## 7. 错误处理与幂等

- 数据库整体不可达时接口返回 503 和 retryable 代码；
- 单个非关键子源失败时 workspace 保留可用部分，同时返回 blocker/stale 证据；
- Run 触发继续使用 `Idempotency-Key`；重复请求返回同一 logical Run；
- 不将空数据包装成成功候选。

## 8. 状态迁移与生命周期

- Run 继续使用 queued/running/cancellation_requested/terminal 单调状态；
- lifecycle stage 是读模型，不是新增可写工作流表；
- recommendation -> approved -> gate -> release/apply -> observation/rollback 的现有状态机不变；
- `no_eligible_candidate` 是正常终止结果，不触发应用。

## 9. 缓存与性能

- Dashboard snapshot plane 只维护一份 `rdpWorkspace` 快照；
- workspace assembler 在一次请求内复用同一份 control summary，并复用一次 Phase 3/4 证据读取；
- active Run 存在时定向刷新，空闲时遵守 TTL/stale/hard-expire；
- 列表有硬上限，不返回无界日志或 artifact 内容。

## 10. 日志、监控与审计

- 保留 Run/Attempt/Step/Event 和 recommendation/release/apply history；
- workspace 返回 daemon heartbeat、execution capacity、queue position 和 stale/error 摘要；
- 新增 workspace build failure 结构化日志，不记录秘密；
- 发布后使用容器健康、`/system/health`、`/system/recovery` 和 RDP Run 事件联合验证。

## 11. 测试策略

- unit：workspace contract、queue projection、lifecycle、capability、candidate eligibility、错误降级；
- API：认证、响应 schema、无凭证泄露、旧 API 兼容；
- frontend：中文文案、队列原因、紧凑空状态、动作 disabled reason、drawer、键盘与 reduced-motion 契约；
- 回归：Ruff、全量 unit、最窄 integration、JS syntax、Markdown links、`git diff --check`；
- 运行：derivatives 模拟发布后人工触发 full RDP 并监控终态。

## 12. 迁移、回滚与兼容

- 无数据迁移；
- 旧 API 保留，前端一次切换到 V3；
- 回滚时恢复旧 `viewSpecs` 和 RDP 渲染器，不需要数据修复；
- 不删除历史文档或外部可能使用的 route。

## 13. 配置与环境隔离

- 不新增 `.env` 开关；
- workflow capability 始终从当前 JSON + code freeze 计算；
- 发布 profile 固定为 `derivatives`；
- live profile 保持在任何外部副作前失败。

## 14. 代码组织与依赖

- 新后端读模型位于 `aats/api/rdp_workspace.py`；
- V3 route 位于独立 router 并由 `rdp_router` 组合；
- 前端按 formatters / lifecycle / runs / research / release 拆分，原巨型渲染器收缩为组装层；
- 不增加第三方运行时依赖。

## 15. 文档与运维手册

- 更新 `docs/rdp/README.md`、`module_reference.md`、Operator SOP、platform runbook 和 workflow calendar；
- 新增本设计、SOW 和最终 code review 报告；
- 明确静态、测试、现场事实和未知边界。

## 16. 部署与验收标准

1. 全部必要测试通过，工作树经复审无未修复问题；
2. 按文件精确暂存，提交 main 并推送 origin；
3. 只使用 `scripts/deploy.sh --profile derivatives --skip-commit` 标准发布；
4. Gateway、market、decision、execution、rdp-daemon 容器健康，并完成多层系统检查；
5. 目标浏览器中手动验证布局、动作、队列解释、Run drawer 和错误状态；
6. 手工触发 `research_cycle`，监控至终态；
7. 只有存在完整、已批准、Gate 通过的模拟候选时才应用，并验证 active parameter/history/runtime provenance/observation；否则记录 `no_eligible_candidate` 并停止。
