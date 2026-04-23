# Golden Path Execution Truth Snapshot Ref Plumbing SoW

## 1. Business objectives and boundaries
- 目标：把 decision 层已有的 `market_snapshot_ref / feature_snapshot_ref / portfolio_snapshot_ref / health_snapshot_ref` 接入 execution truth chain，形成 submit/fill 侧可直接消费的锚点，为后续订单前后盘口快照关联打基础。
- 这次修复必须覆盖真实 submit path，而不只是下游持久化。最短闭环是：
  - `DecisionContext -> PositionTarget -> ExecutionPlan/LegExecutionPlan`
  - `-> OrderIntent/LegOrderIntent -> OrderState`
  - `-> execution_order/raw_payload -> FillEvent/raw_payload`
- 边界：只做 refs 的 schema/plumbing/persistence，不实现新的盘口抓取、不改 live 策略逻辑、不改 execution realism 模型。

## 2. Module responsibilities and domain model
- Decision/strategy 层已经产生 snapshot refs。
- `DecisionContext` 已有四类 refs；`PositionTarget` 是当前 execution planner 的主输入，必须成为上游承接点。
- `ExecutionPlan / LegExecutionPlan` 必须携带这些 refs，否则 submit-time `OrderIntent` 仍会丢失锚点。
- Execution truth chain（OrderIntent -> OrderState -> execution_order/raw_payload -> FillEvent/raw_payload）应携带这些 refs，便于事后归因与对账。
- 本次不新增新快照事件，只传递现有 refs。

## 3. Input/output interfaces
- 可以扩展内部 schema（`PositionTarget / ExecutionPlan / LegExecutionPlan / OrderIntent / LegOrderIntent / OrderState / FillEvent`）增加可选 snapshot refs 字段。
- 不改变对外 API 路径；新增字段必须 backward-compatible、默认 None。

## 4. Database schema / tables / indexes / constraints
- 优先不做 Postgres 列迁移；先落入 JSON/raw_payload 与 schema model，保持兼容。
- 若现有三重持久化约束涉及 OrderState，则 JSON payload 与生成路径必须同步。

## 5. Transactions, consistency, concurrency
- 所有 refs 只是在既有事务中一并写入，不改变事务边界。
- 同步 outbox / execution_order seed / fill row raw_payload，避免 truth chain 断层。

## 6. Authorization, authentication, data security
- refs 只是 event id，不涉及凭证。

## 7. Error handling and idempotency
- 缺 ref 时保留 None；不得因旧路径无 ref 而抛错。
- 旧测试夹具不提供新字段时仍应工作。

## 8. State transition and lifecycle
- submit-time `PositionTarget` / `ExecutionPlan` / `LegExecutionPlan` / `OrderIntent` 必须带 refs。
- `OrderManager` 和 adapter 在创建/恢复 `OrderState` 时必须保留 refs，不能只依赖 raw_payload 猜测。
- `execution_repo_converged_postgres` 在 hydrate / synthetic refresh / order_state -> intent 回转路径上也必须保留 refs；否则 live truth rows 经 DB 层再水化后会再次断层。
- order state 持久化与 execution_order seed 应保留 refs。
- fill event / raw_payload 如能从上游拿到 refs，也应保留；拿不到则至少不破坏旧行为。

## 9. Caching and performance
- 仅增加少量 payload 字段，无明显性能风险。

## 10. Logging, monitoring, auditing
- 目标是让 execution truth rows 和 raw_payload 能直接关联 decision snapshots。
- 如已有 operator/read model 通过 raw_payload 暴露详情，应自然带出这些 refs。

## 11. Testing strategy
- 最窄测试覆盖：
  - `PositionTarget / ExecutionPlan / LegExecutionPlan / OrderIntent / OrderState / FillEvent` 新字段默认 None
  - planner `build_plan / build_leg_plan / build_intent / build_leg_intent` 产物保留 refs
  - bootstrap 主调用链会把 `PositionTarget` 上的 refs 传进 planner
  - `OrderManager` / adapter 创建或恢复 `OrderState` 时保留 refs
  - `execution_repo_converged_postgres` hydrate / synthetic refresh / `_intent_from_order_state` 保留 refs
  - submit seed raw_payload 包含 refs
  - outbox / fill repo raw_payload 保留 refs
  - 旧夹具不传 refs 仍通过
- 必跑：相关最窄 unit + 全量 tests/unit + ruff。

## 12. Migration, rollback, compatibility
- 无 DB migration。
- 回滚只需回退 schema 与 plumbing 改动。

## 13. Configuration and environment isolation
- 无配置改动。

## 14. Code organization and dependencies
- 允许改动：
  - `aats/schemas/decision.py`
  - `aats/schemas/execution.py`
  - `aats/services/decision_engine/target_position.py`
  - `aats/services/execution_engine/planner.py`
  - `aats/bootstrap/config.py`
  - `aats/services/execution_engine/order_manager.py`
  - `aats/services/execution_engine/okx_adapter.py`
  - `aats/storage/execution_repo_converged_postgres.py`
  - `aats/services/execution_control/order_service.py`
  - `aats/services/execution_control/shadow.py`
  - `aats/services/execution_engine/outbox.py`
  - 相关最窄单测
- 本轮明确 **不** 扩到 `paper_adapter.py`；paper-local 路径不在当前黄金路径优先级内，避免范围失控。
- 不碰 live deployment 与 external API。

## 15. Documentation and operations manual
- 如需，仅在代码注释中简要说明 refs 是 execution truth anchor。

## 16. Deployment and acceptance criteria
- 不 deploy。
- 验收：
  - 真实 submit path 上新创建的 `OrderIntent`/`LegOrderIntent` 默认就带四类 snapshot refs
  - `OrderState` / `execution_order.raw_payload` / `FillEvent.raw_payload` 可稳定保留这四类 refs
  - 没有 refs 的旧路径仍兼容
  - 所有相关最窄单测通过
