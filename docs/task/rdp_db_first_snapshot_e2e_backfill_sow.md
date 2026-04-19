# RDP DB-First Snapshots, E2E, And Backfill SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business Objectives And Boundaries

- 将治理快照 `artifact_index`、`active_round_index`、`quality_monitor_summary` 从文件真源切换为 DB-first。
- 将 `step2/phase3/phase4` 的 round summary / manifest 标准化入库，避免 research 链路继续反复读取 round JSON。
- 收紧 `/rdp/control-summary` 返回字段，只保留当前 UI 真正在消费的核心字段。
- 增加浏览器级 E2E，覆盖真实前端按钮点击、请求发送与页面刷新。
- 执行两类一次性历史回填：
  - `rolled_back` release 的 effectiveness 重评
  - Independent 全 blocked bundle 从 `review_required` 回填为 `blocked`
- 在回填后再次复审 Independent 合约实盘链路，并修复新增问题。

边界：

- 继续保留 JSON/Markdown artifact 作为审计副本，不在本轮移除。
- 不改变人工审批边界；自动调优、审批、发布语义保持现有设计。
- 不做无关 UI 重构，不改变公开 API 路由名。

## Module Responsibilities And Domain Model

- `aats.data_platform.governance.snapshot_db`
  - 治理快照和标准化 round snapshot 的 DB 读写。
- `aats.data_platform.governance.artifact_index`
  - 继续构建 artifact index，同时将快照写入 DB。
- `aats.data_platform.governance.round_status`
  - 生成 active round index，并写入 DB。
- `aats.data_platform.governance.quality_monitor`
  - 生成 quality monitor summary，并写入 DB。
- `scripts/rdp_run_step2_research.py`
  - 写入 phase2 step2 的标准化 round snapshot。
- `scripts/rdp_run_phase3_round.py`
  - 写入 phase3 的标准化 round snapshot。
- `scripts/rdp_run_phase4_round.py`
  - 写入 phase4 的标准化 round snapshot。
- `aats.data_platform.decision_system.evidence_bundle`
  - 改为 DB-first 读取治理快照和 round snapshot。
- `aats.services.operator.rdp_queries`
  - 改为 DB-first 读取 attribution / execution realism / health。
- `aats.api.rdp_control_summary`
  - 只返回 UI 使用字段。
- `scripts/rdp_backfill_release_effectiveness.py`
  - 一次性重评 rolled_back release effectiveness。
- `scripts/rdp_backfill_independent_blocked_bundles.py`
  - 一次性回填 Independent 全 blocked bundle。

## Input And Output Interfaces

- Governance snapshot input:
  - artifact roots、round manifests、quality checks、registry state。
- Governance snapshot output:
  - DB snapshot row
  - JSON artifact export（保留）
- Round snapshot input:
  - round manifest、summary JSON、结论文档、关键组合结果。
- Round snapshot output:
  - `governance.research_round_snapshots`
  - 原始 round artifact（保留）
- Control summary output:
  - `environment`
  - `health`
  - `operations_summary`
  - `tasks`
  - `pending_recommendations`
  - `active_parameters`
  - `governance_state`
  - `recent_gate_results`
  - `observation_queue`
- E2E output:
  - 浏览器级按钮点击、请求体、页面状态变化验证。

## Database Schema / Tables / Indexes / Constraints

新增或扩展：

- `governance.snapshots`
  - `snapshot_type` unique
  - `generated_at`
  - `payload` JSONB
- `governance.research_round_snapshots`
  - `round_id` unique
  - `phase` indexed
  - `finished_at` indexed
  - `status`
  - `round_path`
  - `manifest_payload` JSONB
  - `summary_payload` JSONB
  - `conclusion_payload` JSONB
  - `artifacts_payload` JSONB
- 复用现有：
  - `governance.release_effectiveness`
  - `strategy_execution_bundles`
  - `order_states`

约束：

- 同一 `snapshot_type` 只保留一条当前快照。
- 同一 `round_id` 只保留一条标准化 snapshot。
- 回填脚本必须幂等，可重复执行。

## Transactions, Consistency, And Concurrency

- Governance snapshot 与 round snapshot upsert 使用单事务提交。
- 回填脚本按批次读取、逐批提交，避免长事务。
- Historical bundle backfill 在更新 bundle status 与 payload 时保持同一事务。

## Authorization, Authentication, And Data Security

- 仅使用项目既有 DB 连接配置。
- 不读取、不打印任何凭证明文。
- 不在日志中输出 payload 内敏感字段。

## Error Handling And Idempotency

- DB 不可用时：
  - reader 允许回退到文件副本
  - writer 仍生成 artifact，但必须显式记录 DB 写入失败
- Backfill:
  - 只更新需要修正的记录
  - 已正确记录保持不变
  - 重跑不会造成重复状态迁移

## State Transition And Lifecycle

- Governance snapshots:
  - build -> export file -> upsert DB -> subsequent readers use DB-first
- Research rounds:
  - run phase -> write artifact -> normalize snapshot -> subsequent readers use DB-first
- Release effectiveness backfill:
  - historical rolled_back -> reevaluate -> overwrite old effectiveness result
- Independent bundle backfill:
  - historical review_required(all blocked) -> recompute -> blocked

## Caching And Performance

- `control-summary` 不再回传 UI 未消费的大块历史数据，减小 payload。
- round snapshot 读取最新阶段结果时优先走索引列，不扫目录。
- E2E 只跑最小按钮链路，不引入重型前端测试框架。

## Logging, Monitoring, And Auditing

- snapshot 写入失败记录 warning，保留 artifact export。
- backfill 输出修改统计、跳过统计和异常统计。
- 所有新脚本写明开始、结束、处理数量。

## Testing Strategy

- 单元测试：
  - governance snapshot DB-first reader/writer
  - round snapshot DB-first reader/writer
  - slimmed control-summary payload
  - release effectiveness backfill
  - independent blocked bundle backfill
- 集成测试：
  - WSL2 下最窄 RDP API / workflow / operator 受影响链路
  - 浏览器级 E2E：真实按钮点击链路

## Migration, Rollback, And Compatibility

- 新增表需兼容已有 schema 初始化流程。
- DB-first reader 保留 file fallback，允许平滑迁移。
- control-summary 删除字段时同步更新前端与测试，避免破坏当前 UI。

## Configuration And Environment Isolation

- Windows 使用 `.venv\Scripts\python.exe`
- WSL2 使用 `~/aats-venv/bin/python`
- 不新增额外环境变量依赖，复用现有 RDP/governance DB 解析链。

## Code Organization And Dependencies

- snapshot DB helper 放在 `aats/data_platform/governance/`
- backfill 脚本放在 `scripts/`
- 浏览器 E2E 优先复用 Python 依赖，避免引入新的 JS 工具链

## Documentation And Operations Manual

- 新增 SOW
- backfill 脚本需在代码内自带使用说明
- 若新增 DB 表，需在模型层明确用途与兼容策略

## Deployment And Acceptance Criteria

验收标准：

- `artifact_index`、`active_round_index`、`quality_monitor_summary` 可 DB-first 读取。
- `step2/phase3/phase4` 最新 round 信息无需重新读取 round JSON 即可被下游消费。
- `/rdp/control-summary` 返回字段与当前 UI 消费字段对齐。
- 浏览器级 E2E 能跑通 `approve -> create release -> observation -> rollback`。
- `rolled_back` effectiveness backfill 可成功重评历史记录。
- Independent 历史 `review_required` 全 blocked bundle 被回填为 `blocked`。
- 回填后 Independent 链路复审没有新的 P0/P1 断点，或已当场修复。
