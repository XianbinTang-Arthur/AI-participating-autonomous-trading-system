# Operator Read-Side Remaining Refactor Assessment - 2026-05-09

## Recent Decisions Summary Persistence

`recentDecisions` 已经在 2026-05-08 改为 page-level refs batch read，当前剩余方向是把 dashboard row summary 在决策写入时预存为 `decision_summary_json`。这不是单纯读侧改动，原因：

- `DecisionAuditRecord` schema 当前只保存 refs，不保存派生 summary。
- `decision_audit_records` Postgres model 当前没有 summary column，只有结构化 ref columns 和完整 `payload` JSON。
- `DecisionAuditService` 的每个事件 handler 都会 upsert 新 revision；若加入 summary，需要定义在哪个事件阶段生成、如何处理 partial record、如何回填旧 revision。
- 需要 migration、repo model 同步、内存 repo 兼容、写路径单测、回填/rollback 策略。

结论：本轮不把 `decision_summary_json` 塞进 live 改动。下一轮如要做，应单独设计 migration 和 backfill，避免读侧优化变成审计写路径风险。

## RDP Aggregate Loader

RDP dashboard snapshot 目前已经通过 snapshot-only control summary cache 消除了多 panel 对 `build_rdp_control_summary()` 的重复构建，并在 2026-05-08 增加了 cache 容量/过期边界。剩余方向是把 workbench overview/items/alerts/tuning 在同一 aggregate loader 里一次性生成。

当前边界：

- `build_rdp_workbench_overview/items/alerts` 共用 control summary，但 `tuning_overview/proposals` 走独立 payload builder。
- aggregate loader 会改变多个 public helper 的内部组合方式，测试面较大，但仍可保持 public schema。
- 该方向属于 RDP 工作台内部重构，优先级低于 P0 metrics 和 P2 AI 面板隔离。

结论：本轮保留现有 RDP cache 方案，不做 aggregate loader 重构。若后续 RDP panel 继续成为 >1s 高频热点，再单独实施。
