# NATS retention + bootstrap timing 全局架构治理 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **文档状态**：待审批（2026-04-20 起草）
> **调查依据**：background agent `a5010db6f6e3c61fb` 完整报告（5 问 + 4 意外发现），基于源码追踪
> **关联**：取代 [`aats_events_stream_retention_root_fix_sow.md`](aats_events_stream_retention_root_fix_sow.md)（那份是局部方案 C，本 SOW 是全局根治）
> **用户要求**：路线 β — "从全局思考，让优化真正实现全局优化"，不做局部补丁

> **2026-08-24 现行替代说明**：本文保留为 2026-04-20 历史设计证据，其固定 `aats:runtime:ready:{role}`、readiness fallback 和通过开关绕过 barrier 的内容不是当前行为。现行 FS-016 契约见 [`fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md`](fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md)：四主进程 NATS/hybrid 必须使用部署 generation 的 strict barrier，Redis 异常或超时不得继续 publisher。当前验证边界见 [`../../audit/full_system_2026_08_24/30-fs-016-nats-peer-readiness-remediation.md`](../../audit/full_system_2026_08_24/30-fs-016-nats-peer-readiness-remediation.md)。

---

## 1. 背景 & 为什么要"全局治理"

前期排查：`audit.publish → nats: timeout` 每 2-3 分钟一条，根因 `AATS_EVENTS` stream 99.99% 满触发 compaction 风暴。

**局部视角**（前一 SOW）：改 `max_age=1d` + 排除 `AUDIT_RECORDS`，解决眼前 40% 字节浪费。

**全局视角**（本 SOW）：`RetentionPolicy.LIMITS` 本身就是误用——NATS JetStream 被当成"长期存档"（实际长期存档由 PG event_store 承担）。真正的根治是 `LIMITS → INTEREST`（消息所有 durable consumer ack 后立即 remove），让 stream 回归"hot buffer"本职。

但 INTEREST 切换有**硬阻塞**（agent 已定位），不能凭"改一个字段"完事，需要**多层前置工作**才安全。

---

## 2. Agent 调查的 5 个全局问题摘要

| 问题 | 结论 |
|---|---|
| Q1 跨进程 bootstrap 顺序 | `docker-compose` **无 depends_on**，4 主进程并行启动，存在 market 先 publish 但 decision consumer 未 ready 的竞态。**对 INTEREST 是硬阻塞**。 |
| Q2 DeliverPolicy 分类 | LAST 组（MARKET/FEATURE/... snapshots）INTEREST 下安全；**ALL 组 + 跨进程 = 高风险点**（含 ORDER_INTENTS / POSITION_TARGETS 等真实交易指令）|
| Q3 Hydrate 机制覆盖 | 7 个 cache 都从 **Redis** hydrate（不是 PG event_store 也不是 NATS replay）；但 ORDER_INTENTS / ACCOUNT_BASELINES / POSITION_TARGETS / STRATEGY_SLEEVE_INTENTS **没有 Redis hydrate** |
| Q4 AUDIT_RECORDS 消费路径 | **零 live subscribe** 确认；唯一消费是 PG `event_store.by_decision()` 查询；NATS stream 里的副本 100% 冗余 |
| Q5 双写一致性 | `publish_envelope` 非事务；outbox 保护**只覆盖 execution 3 个 topic + portfolio 2 个 topic**，decision 侧全无 outbox；INTEREST 放大跨进程启动期的"PG 有 NATS 无"故障 |

## 3. Agent 意外发现（独立重要）

| # | 发现 | 严重 |
|---|---|---|
| A1 | `DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS` 代码结构已加（前次 SOW 留下），**但 publish_envelope 没使用它** — 基础设施完成一半 | 🟡 中 |
| A2 | `StreamSpec.retention` 硬编码 `RetentionPolicy.LIMITS`（nats_bus.py:367），改 INTEREST 要改多处 | 🟢 低 |
| A3 | gateway 进程 21 个 dashboard relay consumer（config.py:4489-4531），INTEREST 下 H1 readiness gate 必须覆盖 gateway | 🟡 中 |
| A4 | `_CollectingBus` fan_out + INTEREST + observer 混合订阅 — 边界情况需单测 | 🟢 低 |

---

## 4. 分 Stage 治理（4 个 Stage 独立可合入）

```
Stage B0  (0.5 天)  AUDIT_RECORDS 短路 → 立即释放 40% 字节 · 零风险 · 独立于 INTEREST
   ↓
Stage B1  (2-3 天)  bootstrap readiness gate + operator command validation
   ↓
Stage B2a (1-1.5 天) 危险 topic 拆独立 stream 保留 LIMITS · 安全 topic 切 INTEREST · 最小可行
   ↓
Stage B2b (3-5 天)  decision 侧 outbox 扩展 · 所有 topic 切 INTEREST · 彻底根治
```

**推荐先做 B0（立即 40% 收益）+ B1+B2a（最小可行全局根治）**。总 3-5 天。B2b 视 B2a 落地情况决定是否做。

---

## 5. Stage 详细设计

### Stage B0 — AUDIT_RECORDS publish_envelope 短路（0.5 天）

**问题**：agent 发现 A1 —— 代码里 `DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS` frozenset 已定义，但 `publish_envelope` 未使用它。

**改动**：`aats/bus/nats_bus.py:publish_envelope`（line 1086 附近）加白名单短路：

```python
# ── Persist-only 路径：只 PG event_store.append，跳过 NATS publish ──
# 适用于合规/审计需要落盘但无 live consumer 订阅的 topic。
# stream 里的副本纯冗余（等 TTL 到期），释放 40.9% 字节 +
# 消除 audit.publish → nats: timeout 故障路径。
if envelope.topic in DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS:
    if persist and self._event_store is not None:
        try:
            await asyncio.to_thread(self._event_store.append, envelope)
        except Exception as exc:
            log_event(
                self.logger,
                "event_persistence_failed",
                level="error",
                topic=envelope.topic,
                key=envelope.key,
                persistence_mode=self._persistence_mode,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            if self._persistence_mode == "strict":
                raise
    return  # ← 不走 js.publish，stream 里不再有这个 topic 的新消息
```

**同步**：现有代码已加的 `DEFAULT_CRITICAL_EVENTS_TOPICS` 剔除 `DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS` —— stream subjects 不再 claim `aats.system.audit_records`。

**不变量测试**：`test_stream_specs_cover_all_critical_topics_exactly_once`（test_nats_bus_skeleton.py:822）要改：
```python
# 新不变量 I-8'：
# stream_topics 并集 ∪ DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS == DEFAULT_CRITICAL_TOPICS
```

**新增测试**：
- `test_audit_records_persist_only_no_nats_publish` — mock event_store + js.publish，验证 AUDIT_RECORDS envelope 只调 event_store.append 不调 js.publish
- `test_persist_only_topics_removed_from_stream_subjects` — 验证 stream subjects 不包含 persist-only topic

**部署后一次性 ops**：手工 purge stream 里现存的 `aats.system.audit_records` 消息：
```python
await js.purge_stream("AATS_EVENTS", subject="aats.system.audit_records")
```
立即释放 ~1.75 GiB 字节。

**验证**：
- deploy 后 10 分钟：stream bytes 从 4 GiB 降到 ~2.4 GiB
- 30 分钟：`audit.publish → nats: timeout` 日志清零
- 24 小时：stream bytes 稳态 < 1.5 GiB（max_age=1d + audit 剥离的综合效果）

**独立性**：完全独立于 B1/B2，即使不做 B 其他阶段，B0 也是正确改动。

---

### Stage B1 — 跨进程 bootstrap readiness gate（2-3 天）

**问题**：agent 的 Q1 + H1/H2 硬阻塞——4 主进程无 `depends_on`，market publisher 可能先于 decision/execution consumer 启动，INTEREST 下这段窗口内消息丢失。

**设计**：加**进程间 readiness Redis gate**，publisher 在 WS 启动前阻塞等关键 consumer 就位。

#### B1.1 Consumer ready signal（每进程 subscribe 完成后写 Redis）

**位置**：`aats/bootstrap/config.py::_wire_event_subscriptions` 结尾（line 5128 附近）

```python
# subscribe 全部完成后写 ready gate。consumer 都是 durable，stream_info
# 已返回就说明 JetStream server 已持久化 consumer 配置，跨进程可感知。
ready_key = f"aats:runtime:ready:{process_role}"
await hot_state.set(ready_key, json.dumps({
    "process_role": process_role,
    "consumers": sorted(consumer_names),
    "ready_ts": utc_now().isoformat(),
}))
```

#### B1.2 Publisher 启动前阻塞等关键 consumer（per-role 配置）

**位置**：`aats/services/market_gateway/gateway.py::start`（OKX WS 启动前）

```python
# market 进程 publisher（MARKET_SNAPSHOTS/FEATURE_SNAPSHOTS）启动前，
# 阻塞等 decision + execution 的关键 consumer 就位。
# 超时回退：LIMITS 保留 max_age 兜底（不是硬失败）。
required_consumers = {
    "aats-decision-market_snapshots",
    "aats-decision-features_snapshots",
    "aats-execution-market_snapshots",
    "aats-execution-features_snapshots",
}
await wait_for_consumers(
    js=self._js,
    stream="AATS_EVENTS_MARKET",
    consumer_names=required_consumers,
    timeout_seconds=60,
)
```

类似配置给 decision publisher（ORDER_INTENTS 等）和 execution publisher（FILL_EVENTS 等）。

#### B1.3 gateway 的 21 个 relay consumer（A3 意外发现）

gateway 进程 subscribe 21 个 dashboard relay topic。INTEREST 下这些都必须在对应 publisher 启动前就位。所以 market/decision/execution 的"等 consumer" 列表都必须包含它们的 gateway relay counterpart。

#### B1.4 Operator command request-response（H2 硬阻塞）

`OPERATOR_COMMAND_REQUESTS / RESPONSES` DeliverPolicy=NEW，HTTP handler 侧如果在 consumer bind 前就接受请求 → gateway 永远收不到 response。
**改动**：gateway 的 HTTP handler 在 subscribe 完成前返回 503 "system not ready"，让 frontend 可感知。

**测试**：
- `test_wait_for_consumers_blocks_until_ready` — mock consumer_info 逐次返回 not-found → found，验证阻塞行为
- `test_wait_for_consumers_timeout_falls_back` — 超时后 publisher 继续启动（不硬失败）
- 集成：4 进程启动 timing 变化验证（testcontainers，慢速 decision startup 下 market 确实等到 decision ready 才 publish）

**风险**：
- publisher 启动延迟 30-60s 可接受（市场开盘瞬间允许）
- 超时 fallback 到 LIMITS 保留，不硬失败

**独立性**：本 Stage 可**独立于 B2 落地**——先把 readiness gate 建起来（不改 retention），观察 30 天稳定后再切 B2。

---

### Stage B2a — 最小可行 INTEREST 切换（1-1.5 天）

**设计**：**拆 stream**。危险 topic（ORDER_INTENTS / POSITION_TARGETS / ACCOUNT_BASELINES / STRATEGY_SLEEVE_INTENTS）留在 `AATS_EVENTS_COMMANDS` 新 stream 保持 `LIMITS`（配合 max_age=1d）；其他 observer/audit/dashboard-relay 类 topic 迁到 `AATS_EVENTS_OBSERVABILITY` 新 stream 切 `INTEREST`。

**新增 StreamSpec**：

```python
# 危险 topic：无 outbox 保护 + 无 hydrate cache + 跨进程 publisher/consumer + 真实交易指令
DEFAULT_CRITICAL_COMMANDS_TOPICS: frozenset[str] = frozenset({
    _topics.ORDER_INTENTS,
    _topics.POSITION_TARGETS,
    _topics.ACCOUNT_BASELINES,
    _topics.STRATEGY_SLEEVE_INTENTS,
    _topics.PORTFOLIO_ALLOCATION_DECISIONS,
    _topics.STRATEGY_EXECUTION_BUNDLES,
    _topics.EXECUTION_PLANS,
})
# 这些保持 LIMITS —— 启动竞态时靠 max_age 兜底。
# B2b 落地（decision outbox 扩展）后可以移到 INTEREST。

DEFAULT_AATS_EVENTS_COMMANDS_SPEC = StreamSpec(
    name="AATS_EVENTS_COMMANDS",
    topics=DEFAULT_CRITICAL_COMMANDS_TOPICS,
    max_age_seconds=86_400,
    max_bytes=512 * 1024 * 1024,  # 512 MB（这些量小，不需要大）
    max_msgs=1_000_000,
    max_msg_size=4_194_304,
    retention="limits",
)

# AATS_EVENTS 剩下的：observer/audit/dashboard relay/reconciliation
# 全部切 INTEREST —— 有 hydrate 或有 PG event_store 兜底
DEFAULT_AATS_EVENTS_SPEC = StreamSpec(
    ...
    retention="interest",   # ← 改
)
```

**`publish_envelope` 改**：根据 topic 查 stream 路由（已有机制，新增 AATS_EVENTS_COMMANDS 归属即可）。

**StreamSpec 加 retention 字段**（A2 意外发现）：目前 `StreamSpec.retention = "limits"` 硬编码在 `to_nats_stream_config`，需要改成可变。

**不变量测试**：
- `test_stream_specs_cover_all_critical_topics_exactly_once` 扩展为 3 stream（MARKET + EVENTS + COMMANDS）
- 新增 `test_interest_retention_only_safe_topics` — 验证 INTEREST stream 的所有 topic 都有 hydrate cache 或 PG event_store 兜底

**容量预算**（A2 意外发现延续）：
- AATS_EVENTS_MARKET: 2 GB + AATS_EVENTS: 4 GB (→ 改 INTEREST 后实际 < 200 MB) + AATS_EVENTS_COMMANDS: 512 MB = **6.5 GB**
- nats-server.conf max_file_store = 8 GB → headroom 1.5 GB 仍然够

**验证**：
- deploy 后 observer/audit stream 字节降到 MB 级（consumer ack 就 remove）
- 危险 topic stream 保持 GB 级但在可控范围
- `audit.publish → nats: timeout` 清零
- 生产 30 天无 ORDER_INTENTS 丢失事件

**风险**：
- INTEREST stream 的 readiness gate 必须工作（B1 前置）
- 危险 topic stream 依然受 4GB 限制但量小不到
- 双 stream 让 `publish_envelope` 路由逻辑复杂一点

---

### Stage B2b — 彻底 INTEREST + decision outbox 扩展（3-5 天，可选）

**问题**：B2a 保留了 7 个危险 topic 在 LIMITS stream。长期 cleanliness 不够。要彻底切，必须给 decision 进程加 outbox（和 execution outbox 类似）。

**设计**：
- 新增 `decision_outbox_events` 表（类比 `execution_outbox_events`）
- `decision_engine.orchestrator._run_cycle_body` 的 publish 改走 outbox（PG 事务）
- 独立 publisher loop `flush_pending` 读 outbox 发 NATS，at-least-once 保证

**工时**：3-5 天（含测试覆盖）

**独立性**：可以作为 B2a 之后的独立 SOW，按需启动。本 SOW 不细化 B2b 设计。

---

## 6. 推荐落地顺序

```
Week 1:
  Day 1:  B0 实施 + deploy + 观察 stream bytes 降到 < 1.5 GiB
  Day 2-3: B1 设计 + 测试写完
  Day 4-5: B1 deploy + 观察 startup timing

Week 2:
  Day 6-7: B2a 实施 + 测试
  Day 8:  B2a deploy + 观察 observer stream 降到 MB 级

后续（按需）:
  B2b 作为独立 SOW，decision outbox 扩展
```

**最小交付**：B0 + B1 + B2a = 3-5 天，彻底解决 `audit.publish → nats: timeout` + 给 observer topic 实现真正的 hot buffer 语义 + 危险 topic 用分 stream 隔离保护。

---

## 7. 风险矩阵

| Stage | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| B0 | publish_envelope 改动影响其他 persist-only topic（未来加入时）| 低 | 中 | 白名单机制，只有显式列入才短路 |
| B0 | purge_stream 丢 audit_records 老消息 | 高 | 低 | 这些消息没 consumer，丢无影响；event_store 有完整副本 |
| B1 | publisher 等 consumer 超时，交易延迟启动 | 中 | 中 | 超时 fallback 到 LIMITS 原行为（不硬失败）|
| B1 | Redis gate key 写失败 | 低 | 低 | 带 retry；fallback 到不阻塞模式（LIMITS 兜底）|
| B2a | INTEREST stream 的 observer topic 跨进程启动仍有 race | 中 | 低 | B1 的 readiness gate 就是针对这个；fallback 到 dashboard "数据稍等" UI |
| B2a | 新 AATS_EVENTS_COMMANDS stream provisioning 失败 | 低 | 高 | 测试 dry-run；deploy 前手动 `js.add_stream` 验证 |
| B2a | 不变量测试漏掉某条 topic | 低 | 中 | 双重测试（单测 + 集成 testcontainers）|

---

## 8. 回滚预案

**B0 回滚**：revert commit；purge 不影响（重启后 audit publish 自然重填，或 stream 里无 audit 也不影响消费）。

**B1 回滚**：设 `settings.startup_readiness_gate_enabled = False`，publisher 不再等 consumer。

**B2a 回滚**：revert StreamSpec 改动 → `retention=LIMITS` + 合并 COMMANDS 回 AATS_EVENTS。**注意**：合并时老 AATS_EVENTS_COMMANDS 的消息会变成两条 stream 都有（duplicate），需要先 purge COMMANDS stream。

---

## 9. 本 SOW 不覆盖（follow-up）

| # | 主题 | 原因 |
|---|---|---|
| 1 | B2b（decision outbox 扩展）| 工期 3-5 天独立 SOW；B2a 已经用分 stream 隔离危险 topic，B2b 是 cleanliness 优化不是安全修复 |
| 2 | event_store PG 的 housekeeping 14d 阈值从未触发 | 独立 follow-up，已记在主会话 JSONL |
| 3 | audit_records payload 60KB 膨胀 | 独立 follow-up，需要改 audit record 结构（delta/event 模型）|
| 4 | publish_envelope 双写一致性（无 outbox 的 critical topic）| B2b 涵盖 decision 侧；其他角色（market / gateway）观察后定 |

---

## 10. 签收条件

- [ ] B0 合入 + 部署后 30 分钟 stream bytes < 2.4 GiB
- [ ] B0 部署后 24 小时 stream bytes < 1.5 GiB + audit timeout 清零
- [ ] B1 合入 + 部署后 4 进程启动 log 都打 `runtime_ready_gate_announced` 事件
- [ ] B1 集成测试模拟"decision startup 延迟 30s"下 market publisher 正确阻塞
- [ ] B2a 合入 + 部署后 observer stream bytes < 500 MB 稳态
- [ ] B2a 生产 30 天无 ORDER_INTENTS / POSITION_TARGETS 丢失事件（查 `aats.order_manager.intent_received` vs publish 侧计数）
- [ ] 所有新增单元测试 + 集成测试绿
- [ ] 前期 SOW `aats_events_stream_retention_root_fix_sow.md` 的 max_age=1d 改动保留（作为 max_age fallback）

---

## 11. 审批记录

| 角色 | 姓名 | 日期 | 意见 |
|---|---|---|---|
| 起草 | Claude | 2026-04-20 | 基于 agent `a5010db6f6e3c61fb` 源码调查 |
| 审核 | @excellentang | — | — |
| 实施 | — | — | — |
