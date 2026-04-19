# RDP Phase 5/6 DB-First 与 Phase 日志透传任务书

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 业务目标与边界

- 目标 1：让 RDP 的 Phase 5/6 结果对 gateway/UI 的可见性不再依赖 `aats-rdp-daemon` 容器本地 artifact 文件。
- 目标 2：让 `research_cycle` / `full_pipeline` 的 phase 级输出能出现在任务日志里，便于直接定位卡点。
- 边界：
  - 不修改 recommendation / decision 算法本身。
  - 不修改 operator API 公开路由。
  - 不引入新的部署入口，继续沿用现有 deploy 流程。

## 模块职责与领域模型

- `scripts/rdp_run_decision_round.py`
  - 继续生成文件产物。
  - 新增 DB snapshot 持久化，作为 gateway/UI 的主读取来源。
- `aats/data_platform/governance/decision_rounds_db.py`
  - 提供 Phase 6 decision round snapshot 的 DB upsert / latest load。
- `aats/services/operator/rdp_queries.py`
  - `query_latest_decision_round()` 改为 DB-first，文件 fallback。
  - workflow freshness 允许使用最新 decision round snapshot 推断 Phase 5/6 已刷新。
- `aats/data_platform/operations/workflow_dispatcher.py`
  - 保留子任务 stdout/stderr 尾部，透传到 workflow report。
- `scripts/rdp_run_scheduled_workflow.py`
  - 输出每个子任务的 output tail，便于 daemon task queue 记录 phase 级摘要。

## 输入 / 输出接口

- 输入：
  - 现有 `research_cycle` / `decision_cycle` workflow 调度不变。
  - 现有 `rdp_run_decision_round.py` 命令行参数不变。
- 输出：
  - 新增 `governance.decision_round_snapshots` 表。
  - `query_latest_decision_round()` 返回结构保持兼容，但优先来自 DB。
  - `rdp_task_queue.log_tail` 中新增 phase 级输出摘要。

## 数据库 schema / 表 / 约束

- 新增表：`governance.decision_round_snapshots`
  - `round_id` 唯一
  - `started_at` / `finished_at`
  - `evidence_summary_json`
  - `parameter_upgrade_candidates_json`
  - `family_timeframe_decisions_json`
  - `promotion_readiness_json`
  - `manifest_json`
  - `conclusion_markdown`
  - `created_at` / `updated_at`
- 索引：
  - `round_id` 唯一约束
  - `finished_at` 索引，便于取最新 round

## 一致性 / 事务 / 并发

- decision round 文件产物仍照旧写盘。
- DB snapshot 作为同一轮结果的附加持久化，upsert 采用 `round_id` 幂等。
- gateway/UI 读取 latest round 时优先走 DB，避免 daemon 与 gateway 文件系统不共享时出现旧数据。

## 授权 / 安全 / 配置隔离

- 继续复用 governance DB 连接。
- 不新增新的敏感配置项。
- 不输出任何凭证或 `.env` 内容。

## 错误处理与幂等

- DB snapshot upsert 允许同一 `round_id` 重复写入。
- workflow 子任务输出只保留尾部摘要，避免日志无限膨胀。
- 若 DB-first 读取失败，仍保留文件 fallback，避免回归。

## 状态流转与生命周期

- `research_cycle` 成功后，若内部 full pipeline 完成 Phase 6：
  - recommendation / active decision registry 更新
  - decision round snapshot 更新
  - UI 即可读取最新 round
- 任务日志从“只知道 full_pipeline 成功”升级为“可看到 full_pipeline 最后几段 phase 输出”。

## 性能 / 缓存

- snapshot 读取只取最新一条。
- workflow 子任务日志只保留 tail，不引入大体量 stdout 全量存储。

## 日志 / 监控 / 审计

- `workflow_dispatcher` 报告增加 `output_tail` / `stderr_tail`
- `rdp_run_scheduled_workflow.py` 打印这些 tail，供 daemon / task queue 保存

## 测试策略

- 单测：
  - latest decision round DB-first 读取
  - workflow dispatcher 保留 output tail
  - scheduled workflow 打印子任务 tail
  - workflow freshness 可由 decision round snapshot 推断
- 集成：
  - 复用最窄 operator/dashboard 路径，验证 RDP control summary 不回归

## 迁移 / 回滚 / 兼容性

- 通过 `rdp_models.create_all()` 自动建新表。
- 不删除旧文件读路径，保留 fallback。
- 旧 UI / API 结构不变。

## 代码组织与依赖

- 新 DB 读写逻辑收敛到 `aats/data_platform/governance/decision_rounds_db.py`
- 不在 UI 层拼 DB 查询逻辑

## 运维与验收

- 验收标准：
  - 在无共享 artifact 挂载的 daemon 路径下，Phase 6 结果仍能被 gateway/UI 读到
  - task queue 日志中可见 `full_pipeline` 的 phase 摘要
  - 现有 dashboard bundle / RDP API 不回归
