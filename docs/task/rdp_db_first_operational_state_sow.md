
> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

﻿# RDP DB-First Operational State Closure SOW

## 业务目标与边界
- 目标 1：消除 RDP 治理/运营链路对本地 JSON registry 的主依赖，统一以 research/governance DB 作为控制状态真源。
- 目标 2：保留现有 artifact JSON/Markdown 作为导出与审计产物，但不再要求后续流程依赖这些文件才能继续运行。
- 目标 3：在不改变现有 API 结构和主要脚本入口的前提下完成 DB-first 收敛，避免 UI、运维脚本和自动化再次分叉。
- 边界：
  - 不重写 Phase 2/3/4 的研究算法本身。
  - 不删除现有 artifact 输出；artifact 仅降级为导出层。
  - 不把所有 round 级诊断逐项结构化入库；只处理会被流程再次读取、驱动后续行为的控制状态。

## 当前问题摘要
- recommendation / active decision 已是半 DB-first，但 API 写入和部分索引仍绕文件。
- release history、gate result、observation、rollback recommendation、release effectiveness、workflow scheduler state、workflow run report、strategy tuning proposal/overrides 仍以 JSON 为真源。
- operator/query/UI summary 与 metrics 模块继续直接读取 artifact JSON，导致容器/进程间状态不一致。
- health check 存在表名错误，导致 DB 状态被低估。

## 目标模块与职责
- `aats/data_platform/rdp_models.py`
  - 扩展 governance schema，增加运营状态表。
- `aats/data_platform/governance/operational_state_db.py`
  - 封装 release / gate / observation / rollback / effectiveness / workflow run / scheduler / evidence bundle 的 DB CRUD。
- `aats/data_platform/governance/strategy_tuning_db.py`
  - 封装 tuning proposal 的 DB CRUD，并从 approved proposal 派生 combo overrides。
- `aats/data_platform/production_workflow/*`
  - 保持现有接口，但 load/save 改为 DB-first。
- `aats/data_platform/operations/*`
  - scheduler、dispatcher、strategy tuning registry 改为 DB-first。
- `aats/api/rdp_routes.py`
  - 写接口不再显式绑定 JSON 文件路径。
- `aats/api/rdp_control_summary.py`
  - Recent gate/release/observation 队列改为 DB-first。
- `aats/services/operator/rdp_queries.py`
  - workflow run / evidence index / health query 改为 DB-first，修复错误表名。
- `aats/data_platform/metrics/*`
  - release/effectiveness/workflow/apply-history 相关统计改为调用 DB-first loader，而不是手开 JSON。

## DB Schema 设计
- 新增表：
  - `governance.workflow_run_reports`
  - `governance.workflow_scheduler_state`
  - `governance.pre_apply_gate_results`
  - `governance.parameter_releases`
  - `governance.observation_results`
  - `governance.rollback_recommendations`
  - `governance.release_effectiveness`
  - `governance.strategy_tuning_proposals`
  - `governance.decision_evidence_bundles`
- 设计原则：
  - 每张表均保留业务主键和必要的查询索引。
  - 对需要兼容旧 JSON 结构的复杂字段使用 `JSONB payload` 保存，避免迁移时丢上下文。
  - 对高频查询字段单独建列，如 `workflow`、`combo_key`、`release_id`、`status`、`finished_at`。

## 输入 / 输出契约
- 输入不变：
  - `rdp_run_decision_round.py`
  - `rdp_run_release_cycle.py`
  - `rdp_schedule_workflows.py`
  - `rdp_review_strategy_tuning_proposal.py`
  - operator API 路径
- 输出增强：
  - load/query 接口优先读 DB，文件仅 fallback。
  - save/update 接口优先写 DB，同时继续导出 JSON/Markdown artifact。
  - metrics 和 UI summary 对外返回结构保持兼容。

## 事务、一致性与幂等
- recommendation / active decision / release / tuning proposal 等状态写入都使用唯一业务主键做 upsert。
- release / observation / rollback / effectiveness 对单个 `release_id` 采用唯一键，重复执行时覆盖最新结果。
- scheduler state 采用 `workflow` 唯一键，避免重复窗口处理。
- workflow run report 采用 `run_id` 唯一键，支持重复保存同一运行结果。

## 兼容性策略
- 所有 load_* 接口保留原始返回结构，调用方尽量无感知迁移。
- 仅在 DB 不可用或新表不存在时回退文件。
- 继续输出原 JSON artifact，保证已有审计习惯和旧测试样例可平滑过渡。

## 错误处理
- DB 写失败时：
  - 关键控制状态优先记录 warning，并继续写 artifact，避免运行中断。
  - 对必须读取最新状态的接口，若 DB 读取失败再 fallback 到文件。
- health/query 层统一把 DB 读取异常降级为 warning，不静默吞掉真实错误上下文。

## 测试策略
- 单测：
  - workflow scheduler state DB-first 行为
  - strategy tuning proposal DB-first 审核/override 行为
  - release history / observation / rollback / effectiveness DB-first loader 行为
  - rdp queries / control summary / routes 不再依赖本地 JSON
  - health check 错误表名修复
- 集成：
  - `test_rdp_production_workflow_api.py`
  - 受影响的 dashboard/control summary smoke

## 验收标准
- UI/API 在无共享 artifact 挂载、仅有治理 DB 的环境中仍可读取 recommendation、release、observation、scheduler、tuning 状态。
- `Run Research -> Decision -> Release Cycle -> Observation -> Rollback Eval` 的控制状态不再以 JSON 为必需输入。
- strategy tuning 不再依赖 `strategy_tuning_overrides.json` 才能影响后续 replay/scan/research 默认值。
- health check 与 workflow freshness 不再因本地文件缺失而误报。
