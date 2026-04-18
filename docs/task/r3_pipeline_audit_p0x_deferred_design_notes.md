# R3 四链路代码审查 — P0-X1 / P0-X2 延后决议与设计笔记

> 归属：R3 合约实盘链路审查（行情→决策→执行→跨进程消息）
> 决策日期：2026-04-17
> 状态：本轮 **不实现**，留作设计笔记 + 后续 SOW 输入
> 前提：本轮已修复 5 个 P0（M1/D1/D2/E1/E2），详见同批 commit

## 背景

R3 审查列出 7 个 P0 候选。其中 M1/D1/D2/E1/E2 已在本轮修复；
X1（inbox 模式 / 全局 event_id 消费幂等）与 X2（NATS subject 分区键）
**属于架构级改动**，直接实现将：

1. 需要新建 Postgres 表（per-consumer inbox tracking）并做在线迁移；
2. 需要更改 `bus/nats_bus.py` 的订阅模型（subject 分区扩展）；
3. 风险面覆盖 4 个进程、3 个数据库表、NATS 消费者组；
4. 不满足 CLAUDE.md "备份+设计+审批" 三步走纪律。

按用户"质量优先于速度"原则，本轮改为：**先把 P0 阻塞项修完，
把 X1/X2 留成独立 SOW**，避免把架构改动挤进 bugfix 批次。

## P0-X1：跨进程 event_id 消费幂等（inbox 模式）

### 当前风险

NATS JetStream 的 `duplicate_window` 只对 publish 侧去重，消费侧
消费确认不等于幂等执行。具体路径：

- `aats/bus/nats_bus.py::_on_msg`（line ~1105-1243）收到 msg 后：
  1. 反序列化 envelope
  2. （可选）写 event_store / stream_cache
  3. 调用 handler
  4. ack / nak
- 如果 handler 执行完成但 ack 发送前进程崩溃，NATS 将在 `ack_wait`
  超时后 redeliver；handler 将**再跑一次同一条 event_id**。

现有防线（已验证）：

| 位置 | 幂等机制 |
|------|---------|
| `storage/event_store_postgres.py::append_in_session` L37-42 | `event_id` 存储层去重 |
| `services/execution_engine/outbox.py::persist_order_state_with_fills_sync` | OrderState OCC `row_version` + fill_id 去重（本轮 P0-E2 已修） |
| `services/execution_engine/order_state_cache.py::_reconcile_bootstrap_truth` | 时间戳比较跳过旧值（本轮 P0-E1 已修） |
| `services/portfolio_service/outbox.py` | DB 事务 + snapshot upsert |
| `services/execution_engine/okx_rest_order_repo.py` | client_order_id natural key |

**结论**：payload 层已有业务幂等兜底，存储层有 event_id 去重；
**handler 重复执行的剩余风险**集中在：
- 外部副作用（例如冷路径里向 OKX REST 发单 — 本系统通过
  client_order_id 去重，风险受控）；
- 日志/指标重复计数（运维可察觉，不影响资金）。

### 延后理由

1. **实现代价**：需要新表 `nats_consumer_inbox(consumer_group, event_id, processed_at)`，
   必须覆盖所有订阅者（market/decision/execution/portfolio/gateway），
   每个 handler 都要包到 `with inbox.claim(event_id):` 事务里。
2. **迁移代价**：生产库需在线迁移 + 回填历史 processed ids（或接受从零计数）。
3. **测试代价**：每条订阅都需要重复投递幂等测试。
4. **当前防线已够**：上述每条关键路径已有业务层幂等，
   真正的资金风险点已封死。

### 后续设计要求（下一轮 SOW 输入）

- 表结构：`(consumer_group text, event_id text, processed_at timestamptz, primary key(consumer_group, event_id))`；
- 清理策略：`processed_at < now() - interval '7 days'` 后删除，
  需比 NATS `duplicate_window` 长；
- 封装：`aats/bus/inbox.py` 暴露 `claim(event_id) -> contextmanager`，
  进入时 INSERT ... ON CONFLICT DO NOTHING，RETURNING 判断是否是
  首次；退出时 commit；
- 切入点：在 `_on_msg` 里 handler 调用之前 `claim`；
- 回归测试：在 `tests/integration/` 新增"同一 event_id 两次投递"用例。

## P0-X2：NATS subject 分区键

### 原审查主张

"NATS 单一 subject 只有一个 consumer 并发度，存在 symbol 级别乱序风险。"

### 复核结论 — **false positive**

读 `aats/bus/nats_bus.py::subject_for(topic)`：

```python
def subject_for(self, topic: str) -> str:
    return f"{self._subject_prefix}{topic}"
```

以及 JetStream consumer 配置：单 subject 单 consumer + `max_ack_pending`
控制 in-flight。

**关键事实**：NATS JetStream 对**同一 subject** 提供 **total order** 语义，
消息按 publish 顺序严格递送。在当前 1 subject / topic 的拓扑下，
同 symbol 的 fill/orderstate 永远走同一 subject，天然有序，
**不需要 Kafka 式 partition key**。

真正要分区的前提是"单 subject 吞吐不足要横向扩展 consumer"，
这时才需要把 subject 拆成 `aats.fills.btc` / `aats.fills.eth` 等
symbol-scoped subjects。当前系统吞吐 < 100 msg/s（衍生品实盘单品种），
**完全够用**，不需要此改造。

### 延后理由

X2 不是 bug，是**可选的 scaling 方案**。当前吞吐不需要，
改造会引入 wildcard subscription 复杂度、subject 发现机制等额外
运维成本。标记为 **后续 scaling 触发项**，阈值：

- 单进程 NATS consumer 积压 > 1000 msg 持续 5 分钟，或
- 单 subject p99 端到端延迟 > 500ms。

上述任一触发即重新评估。

### 后续设计要求（若触发）

- topic → subject 映射表改为 `{prefix}{topic}.{partition_key}`；
- `partition_key` 由 envelope.key 哈希或直接取 symbol；
- 订阅端用 wildcard `{prefix}{topic}.*`；
- 每个 subject 独立 consumer。

## 本轮 P0 整改清单（已完成）

| 编号 | 位置 | 修复 |
|------|------|------|
| P0-M1 | `services/market_gateway/okx_websocket.py` | 订阅 ack/error 限定到来源连接，避免跨连接误配 |
| P0-D1 | `services/decision_engine/target_position.py::_leverage_bias` | factor_scores 读取包 `math.isfinite` 防 NaN 泄漏 |
| P0-D2 | `services/decision_engine/target_position.py::_decision_outcome` | `policy_blocked` / `risk_capped` 由实际 blocker 列表派生 |
| P0-E1 | `services/execution_engine/order_state_cache.py::_reconcile_bootstrap_truth` | PG truth 比 cached 旧时跳过覆盖 |
| P0-E2 | `services/execution_engine/outbox.py::persist_order_state_with_fills` | 仅广播 **本次新入库**的 fills 到 cache |

5 项合计 test impact：market(14) + decision(60) + outbox(10) = 84 单测通过。

## 下一步

- 本轮继续 P1 批次（M2/M3/D3-D5/E3-E5/X3-X5/U-A-D）。
- X1/X2 作为独立 SOW 立项（参考本文件 §"后续设计要求"）。
- 若运维在 X1 防线失效场景观察到资金影响，立即升为 P0 回填。
