# Task119 Parent Review And Serial Exit Resume SOW

## 业务目标与边界
- 将 `parent-child exit aggregation` 的 review / operator 语义上收到现有 `reconciliation -> recovery_view` 链路。
- 让串行 `max-size` 退出拆单不再只停在“首次提交流程内”的保守串行，而是在后续 `sync_exchange_state()` 后基于 parent 聚合状态安全续派。
- 本轮仍不实现：
  - 并行 child 扇出
  - parent / child 的数据库持久化 schema
  - 独立的 operator review 表或控制面板

## 当前行为摘要
- parent exit intent 已能聚合 child `OrderState` 并在 reconciliation 后重算。
- recovery / operator 面当前主要看到 child unknown-write 详情，缺少 parent 级 review / resume-block 信息。
- 串行退出拆单目前只会在首次 submit 流程里连续提交；一旦某个 child 先进入 `SUBMITTED/WORKING`，后续 child 需要人工再次触发，系统不会在后续 sync 后自动续派。

## 计划改动

### 1. parent review / operator 汇总
- 在 `aats/services/execution_engine/exit_intent_aggregator.py` 增加 parent resume 判定与 reconciliation report augmentation helper：
  - 识别 `dispatch_template_missing`
  - 识别 `parent_review_required`
  - 生成 parent 级 `unknown_state_details` / `findings`
  - 回写 `recommended_operator_action`
- 在 `aats/services/reconciliation_service/repair.py` 中：
  - 先 refresh parent truth
  - 再将 parent review overlay 合并进 `ReconciliationReport`
  - 重新走 classifier 注解
  - 让 recovery / operator 查询天然看到 parent 级 review 信息

### 2. serial exit split 恢复 / 续派
- 在 `aats/services/execution_engine/order_manager.py` 中：
  - 给 parent metadata 注入可重建的 dispatch template
  - 在 `sync_exchange_state()` 刷新 parent 后识别 resumable parent
  - 当 parent 满足：
    - risk-reducing
    - 非 terminal / 非 review / 非 cancel_requested
    - `remaining_dispatchable_quantity > 0`
    - 无 `open_child_working_quantity`
    - 无 `open_child_unknown_quantity`
    - 存在 dispatch template
    - venue `max-size` limit 仍可获取
  - 则自动提交下一张 child
  - 若 child 立即终态，允许继续串行续派；若 child 再次进入 live / unknown，则停止继续自动派发

### 3. 测试
- unit:
  - reconciliation report 能透出 parent review detail
  - parent 缺失 dispatch template 时进入 review overlay
  - first child live -> sync fill -> 自动续派下一张 child
- narrow integration:
  - guarded/live OKX close leg 在“先 live、后 sync”路径上能继续串行拆单

## 回滚与兼容
- 不注入 `exit_execution_repo` 时，新逻辑自动退化为空操作。
- 没有 dispatch template 的 parent 不会自动续派，只会通过 recovery / operator 面透出 review 信号。
