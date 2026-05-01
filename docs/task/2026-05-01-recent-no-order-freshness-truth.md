# Recent No-Order Freshness Truth

## Business Objectives And Boundaries

本轮目标是在 runtime truth 中补一层只读证据：确认最新 directional decision 仍然新鲜、仍与 recent no-order provenance density gate 指向同一 decision，并且 microstructure runtime 仍然新鲜。范围限定为 OKX + BTC-USDT-SWAP directional canary 的观测面，不改变策略、风控、执行、provider、schema、symbol、venue、release、promotion、tuning 或 live order behavior。

## Module Responsibilities And Domain Model

`scripts/runtime_truth_report.py` 负责从现有 DB truth、recent no-order provenance gate、microstructure runtime growth 三个已脱敏事实源组合出 `recent_directional_no_order_freshness_truth`。该 surface 是 evidence freshness / identity continuity，不是 alpha、盈利性或交易建议。

## Input/Output Interfaces

输入为 `database_truth.latest_decision`、`recent_directional_no_order_provenance_density_gate_truth`、`microstructure_runtime_growth_truth` 和 report generated timestamp。输出新增 `recent_directional_no_order_freshness_truth`，并投影到 `runtime.live_runtime_facts.recent_directional_no_order_freshness_*`；同时补充 `latest_decision_created_at`。

## Database Schema / Tables / Indexes / Constraints

无 schema、表、索引或约束变更。所有 DB 读取仍通过既有 runtime truth probe 在容器内使用隐式环境完成，不打印凭证或连接串。

## Transactions, Consistency, Concurrency

该变更只读、无事务写入、无锁语义变化。consistency 规则为：DB latest decision id 必须与 provenance gate latest decision id 一致，且 latest decision age 不超过 1800 秒。

## Authorization, Authentication, Data Security

不新增认证入口，不读取或输出 secrets、token、API key、数据库密码或完整连接串。新增输出仅包含非敏感 decision id、时间戳、状态、计数和布尔证据。

## Error Handling And Idempotency

缺 latest decision、缺 created_at、created_at stale、gate 未验证、identity mismatch、recent window 有 fills、microstructure 未新鲜时分别给出明确 status 和 smallest_missing_field。重复执行只生成相同类型的只读报告。

## State Transition And Lifecycle

不改变交易生命周期。该 truth surface 只描述当前 no-order evidence regime 是否仍然新鲜、连贯、可审计。

## Caching And Performance

新增逻辑仅组合已有 probe 结果，不增加 DB 查询或网络调用；性能影响可忽略。

## Logging, Monitoring, Auditing

runtime truth report 新增可审计字段：latest decision age、stale threshold、provenance gate status、microstructure heartbeat/payload/silver status。

## Testing Strategy

新增单元测试覆盖 fresh success、stale latest decision、live runtime facts projection。继续运行 focused tests、ruff、full unit tests 和 post-deploy runtime truth。

## Migration, Rollback, Compatibility

无迁移。回滚方式为 revert 本次代码和测试/doc commit；不需要数据或运行态回滚。

## Configuration And Environment Isolation

新增 freshness threshold 为代码常量 `LATEST_DECISION_FRESHNESS_STALE_AFTER_SECONDS = 1800`。不新增环境变量，不改变 live/prod/test 配置。

## Code Organization And Dependencies

变更局限于 runtime truth script、对应单元测试和本任务文档；不新增依赖。

## Documentation And Operations Manual

operator 可在 runtime truth 中查看 `recent_directional_no_order_freshness_truth.status` 和投影后的 `recent_directional_no_order_freshness_*` 字段，判断 latest no-order evidence 是否新鲜。

## Deployment And Acceptance Criteria

Acceptance criteria:

1. Fresh runtime truth 在 PM action 前生成，且 hard_stop=false。
2. 新 truth surface 对 fresh/no-order/microstructure 三者同时 verified 时返回 `verified_recent_directional_no_order_freshness_truth`。
3. stale latest decision 返回明确 missing field `database_truth.latest_decision.created_at`。
4. `runtime.live_runtime_facts` 暴露 latest decision created_at 和 freshness projection。
5. focused tests、ruff、full unit tests、commit/push/deploy/post-deploy runtime truth 全部通过。
