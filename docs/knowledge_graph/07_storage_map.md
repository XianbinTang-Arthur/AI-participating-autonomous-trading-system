# 07 · 存储映射（PG / Redis / NATS / event_store）

> **历史且含现场采样**：采样于 2026-04-21，不代表当前数据或容量。当前表/Redis/Topic/JetStream 以代码和迁移为准，不要传播本文的历史账户/运行数据。

> **生成于 HEAD=待更新** · 2026-04-21
> **内容**：所有的持久化 / 缓存 / 消息通道
> **采样自** 实盘 `aats_live_derivatives`（2026-04-21 22:30 UTC）

---

## TL;DR

AATS 用 **4 种持久化** + **1 种进程间消息**：

1. **Postgres** (`aats_live_derivatives` 数据库) — 52 张表、7.7 GB（event_store 独占 7.7 GB）
2. **Redis** (`aats:hot:*` 命名空间) — 5 个 namespace，TTL 1h~7d
3. **NATS JetStream** (`AATS_EVENTS` stream) — 50 个 topic，关键 topic 文件持久
4. **event_store + event_store_archive** — 持久化全部跨进程事件，14d / 90d TTL
5. **File logs** (`logs/live_derivatives/`) — 日志轮转 5MB × 7

---

## 1 · Postgres（aats_live_derivatives 数据库）

### Top 20 表（按 size）

| 表 | size | 类别 |
|---|------|------|
| **event_store** | 7,715 MB | 事件 source of truth |
| decision_audit_records | 431 MB | 决策审计 |
| strategy_sleeve_intents | 369 MB | sleeve 决策历史 |
| portfolio_allocation_decisions | 248 MB | allocator 历史 |
| reconciliation_findings | 50 MB | 对账 findings |
| strategy_profile_recommendations | 39 MB | 策略 profile 建议 |
| exit_execution_intents | 37 MB | 退出订单意图 |
| strategy_profile_evaluations | 34 MB | profile 评估 |
| reconciliation_reports | 23 MB | 对账报告 |
| exit_execution_child_refs | 12 MB | 退出子订单 |
| outbox_events | 8 MB | outbox pattern |
| sleeve_budget_assignments | 5 MB | 预算分配 |
| order_states | 4.5 MB | 订单状态（热表） |
| order_obligations | 4.2 MB | 占用资金 |
| sleeve_budget_profiles | 3.8 MB | 预算 profile |
| funding_fee_records | 3.1 MB | 衍生品资金费 |
| strategy_profile_revisions | 784 kB | profile 版本 |
| reconciliation_state_snapshots | 784 kB | 对账 snapshot |
| execution_orders | 656 kB | 订单（长周期） |
| execution_fills | 456 kB | 成交（长周期） |

### 按功能分组（全 52 张表）

**事件 / 审计**: `event_store`, `event_store_archive`, `decision_audit_records`, `external_event_inbox`, `command_outbox`, `outbox_events`

**订单执行**: `order_states`, `execution_orders`, `execution_order_state_history`, `execution_commands`, `order_obligations`, `reservations`, `exchange_ack_watermarks`

**成交 / 账本**: `fill_events`, `execution_fills`, `fill_outcomes`, `lot_events`, `position_lots`, `ledger_accounts`, `ledger_entries`, `ledger_journals`, `funding_fee_records`, `settlements`

**退出订单**: `exit_execution_intents`, `exit_execution_child_refs`

**组合 / PnL**: `portfolio_snapshots`, `portfolio_allocation_decisions`, `baseline_generations`, `sleeve_pnl_records`, `projection_replay_offsets`

**对账 / 恢复**: `reconciliation_reports`, `reconciliation_findings`, `reconciliation_state_snapshots`

**策略 / Allocator**: `strategy_sleeves`, `strategy_sleeve_intents`, `strategy_execution_bundles`, `allocator_budget_snapshots`, `allocator_conflict_resolutions`, `allocator_netting_decisions`, `sleeve_budget_profiles`, `sleeve_budget_assignments`

**Strategy Profile**: `strategy_profile_activation`, `strategy_profile_activation_history`, `strategy_profile_evaluations`, `strategy_profile_recommendations`, `strategy_profile_rejections`, `strategy_profile_revisions`

**运维**: `schema_migrations`, `operator_users`

**Backup（临时）**: `execution_orders_backup_20260420_blocked`, `order_states_backup_20260420_blocked`, `fill_events_backup_20260420_orphan`（⚠️ 应该定期清理，见 LF）

---

## 2 · Redis 缓存命名空间

**pattern**: `aats:hot:<namespace>:<key>`

| Namespace | 内容 | TTL | Publisher | Consumer |
|-----------|------|-----|-----------|----------|
| `hot:account` | AccountSnapshot | 30 min | execution | all |
| `hot:fill_event` | 单个 FillEvent | 7 days | execution | decision, gateway |
| `hot:obligation` | OrderObligation | 30 min | execution | all |
| `hot:order_state` | OrderState | 30 min | execution | all |
| `hot:system` | KillSwitch, GuardSignal | 30 days / 6 min | execution / governance | all |

**索引 key** (如 `aats:hot:fill_event:index`) 单独维护，TTL 同 entries。

**注意**: Redis 密码在 `.env.wsl2`，不直接写入代码。

---

## 3 · NATS Topics（50 个）

**命名规范**: `<domain>.<action>`（snake_case）

### 按 domain 分组

| Domain | Topics |
|--------|--------|
| **market** | MARKET_SNAPSHOTS = `market.snapshots` |
| **features** | FEATURE_SNAPSHOTS = `features.snapshots` |
| **system** | HEALTH_SNAPSHOTS, KILL_SWITCH_STATE, OPERATOR_*, PROCESSING_FAILURES, GUARD_SIGNAL_UPDATES |
| **account** | ACCOUNT_BASELINES, ACCOUNT_SNAPSHOTS |
| **strategy** | DECISION_CONTEXTS, BASELINE_ASSESSMENTS, AI_ASSESSMENTS, POSITION_TARGETS, DECISION_OUTCOMES（未发布）, STRATEGY_COORDINATOR_SNAPSHOTS, STRATEGY_SLEEVE_INTENTS, PORTFOLIO_ALLOCATION_DECISIONS, STRATEGY_EXECUTION_BUNDLES, OVERLAY_PARENT_EXPOSURES, AI_*（6 个） |
| **policy** | POLICY_DECISIONS |
| **risk** | RISK_DECISIONS |
| **execution** | EXECUTION_PLANS, ORDER_INTENTS, ORDER_UPDATES, OBLIGATION_UPDATES, FILL_EVENTS, ERROR_SUMMARIES |
| **portfolio** | PORTFOLIO_BALANCE_DELTAS, PORTFOLIO_SNAPSHOTS |
| **reconciliation** | RECONCILIATION_REPORTS, RECONCILIATION_VALIDATIONS |
| **replay** | REPLAY_VALIDATIONS |

### 关键 topic（file-backed）

- `KILL_SWITCH_STATE` 
- `OBLIGATION_UPDATES`
- `ORDER_UPDATES`
- `GUARD_SIGNAL_UPDATES`（已加 dedup）
- `POSITION_TARGETS`, `POLICY_DECISIONS`, `RISK_DECISIONS`, `ORDER_INTENTS`
- `FILL_EVENTS`
- `RECONCILIATION_REPORTS`
- `OPERATOR_COMMAND_REQUESTS/RESPONSES`

### 高频 observer（内存 stream cache）

- `MARKET_SNAPSHOTS`
- `FEATURE_SNAPSHOTS`

这两条 topic 走 `StreamSnapshotCache`（内存），**不**持久化到 Postgres
event_store，避免每小时塞几十万行。

---

## 4 · event_store 是什么

**概念**：所有跨进程事件的统一持久层。相当于 "durable NATS topic"。

**两张表**：
- `event_store`（热表）— 14 天保留，按 event_timestamp 索引
- `event_store_archive`（冷表）— 90 天保留，housekeeping 任务自动迁移

**写入路径**（2026-04-21 本场 dedup 修复后）：
- 业务 publish → NATS publisher → **先 event_store.append 再 NATS publish**
- 相同 payload 哈希的 recovery guard signal 跳过 append（T+5 dedup）

**读路径**：
- Replay engine（对账 / 重放）
- 诊断工具（scripts/diag/event_store_bloat_audit.sh）

**可审计性**: 每个 EventEnvelope 含 trace_context，能在 Jaeger 里关联。

---

## 5 · 日志文件

| 路径 | 内容 | 轮转 |
|------|------|------|
| `logs/live_derivatives/` | 所有进程 JSON 日志 | 5 MB × 7 backup |

Loki 通过 promtail 吸走，Grafana dashboard 查。

---

## 使用建议

### 查一个事件去哪了
1. [02_data_flow.md](02_data_flow.md) 找 topic → producer / consumer
2. 本文档 NATS section 看 topic 全路径
3. 如果 file-backed，也进了 event_store：直接 `SELECT * FROM event_store WHERE event_type=...`

### 看系统在存什么
1. `bash scripts/diag/table_growth_audit.sh 20`
2. `bash scripts/diag/event_store_bloat_audit.sh`
3. 本文档 "按功能分组" 定位表族

### 生产空间爆炸怎么查
1. `bash scripts/diag/housekeeping_health.sh` — archive 有没有跑
2. `pg_total_relation_size` 看 top 消耗表
3. 对 event_store 跑 bloat audit 看是哪个 event_type

---

## 发现的 latent 模式

- **Backup tables 没清** — `*_backup_20260420_*` 3 张表可以删了
- **event_store 依然大头**（7.7 GB / 52 表总量的 >85%），dedup 后增速 -70% 但历史存量仍在，housekeeping 14d 后会自动迁 archive
- **Redis 命名空间扁平** — `aats:hot:*` 下 5 个 namespace，未来多品种多策略时可能需要分层
