# Task 229: submit/ack/fill orderbook lifecycle truth refs

## 背景

当前 `lifecycle_snapshot_refs` 已经有阶段化结构，并预留了
`pre_event_orderbook_snapshot_ref` / `post_event_orderbook_snapshot_ref`：

- `submit`
- `ack`
- `fill`

但运行路径实际只把 decision-time 的四类 refs
`market_snapshot_ref` / `feature_snapshot_ref` / `portfolio_snapshot_ref` /
`health_snapshot_ref` 写入生命周期节点。即使上游 command 或 fill payload 已经携带
pre/post orderbook refs，submit/ack/fill 落库路径也不会统一提取和合并。

这会让 P1 truth chain 看起来有 lifecycle 节点，但缺少事件前后盘口引用，后续无法可靠做
fill feasibility、滑点归因、maker/taker 解释。

## 当前行为

- `lifecycle_snapshot_ref_payload()` 可以保存 pre/post orderbook refs。
- `order_service` / `outbox` / `converged_execution_repo` 调用时只传入四类
  decision refs。
- 缺失盘口 refs 时结构会显示 `capture_status=missing`，这是正确的；问题是有 refs 时
  没有稳定 plumbing。

## 目标

补齐最小可审计链路：

- 从 raw payload / command payload / fill raw exchange 子树中提取 lifecycle market context refs。
- 在 submit / ack / fill lifecycle 节点中保存 pre/post orderbook refs。
- 未提供 refs 时继续显式记录 missing，不伪造捕获结果。

## Input

- `command_payload`
- execution order `raw_payload`
- fill `raw_exchange`
- fill/order `model_dump()` payload
- 已存在的 `lifecycle_snapshot_refs`

## Output

- `execution_orders.raw_payload.lifecycle_snapshot_refs.<stage>.market_context_snapshot_refs`
  能保存：
  - `pre_event_orderbook_snapshot_ref`
  - `post_event_orderbook_snapshot_ref`
  - `capture_status`
  - `missing_refs`
- `execution_fills.raw_payload.lifecycle_snapshot_refs.fill.market_context_snapshot_refs`
  同步保存 fill 阶段 refs。

## 影响范围

只改以下边界：

- `aats/services/execution_engine/lifecycle_snapshot_refs.py`
- `aats/services/execution_engine/outbox.py`
- `aats/services/execution_control/order_service.py`
- `aats/storage/execution_repo_converged_postgres.py`
- `tests/unit/test_execution_truth_snapshot_ref_plumbing.py`

不改：

- DB schema
- strategy family / symbol / venue
- AI enablement 语义
- execution action / risk gate
- runtime timeframe plumbing

## 验证方式

二值验收标准：

1. command payload 携带 pre/post orderbook refs 时，submit lifecycle 节点保存为
   `capture_status=captured`。
2. existing raw payload 携带 pre/post orderbook refs 时，ack lifecycle 节点保存 refs，
   且已有 submit refs 不丢失。
3. fill raw exchange 携带 pre/post orderbook refs 时，fill lifecycle 节点保存 refs。
4. 未携带 refs 时保持 `capture_status=missing`，旧测试不破坏。

## 回滚方式

回滚本任务涉及的上述文件即可。由于不改 schema、不改配置、不改运行开关，回滚不需要迁移。
