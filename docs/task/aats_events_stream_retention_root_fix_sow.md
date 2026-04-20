# AATS_EVENTS stream 保留策略根治 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **文档状态**：待审批（2026-04-20 起草）
> **根因调查**：本 SOW 直接依赖主会话 JSONL 里的源码追踪 + NATS 运行时状态查询
> **工期估计**：0.5 天（3 处代码 + 3 处测试断言 + 部署验证）
> **用户要求**：**根治**，不是扩容 band-aid

---

## 1. 背景 & 现状

### 1.1 症状

Grafana dashboard 暴露：
- `noncritical_subscription_failed error=nats: timeout` 每 2-3 分钟 1 条（S2 部署后仍在）
- handler 分布：`audit.handle_portfolio_allocation_decision / audit.handle_strategy_sleeve_intent / audit.handle_decision_outcome`（全是 audit observer 路径）

### 1.2 查到的事实

**NATS JetStream `AATS_EVENTS` 运行时状态**：
```
msgs=69,834  bytes=4,294,304,852 (99.985% 逼近 max_bytes=4 GiB 上限)
num_subjects=19
retention=LIMITS   discard=OLD   max_age=604800s (7 天)
```

**字节分布（top 3 占 71%）**：
```
aats.system.audit_records          28,568 msgs (40.9%)  ← 40% 的流量
aats.strategy.sleeve_intents       11,996 msgs (17.2%)
aats.system.guard_signal_updates    9,299 msgs (13.3%)
```

**audit_records 的 live consumer 数量**：**0**（66 个 durable consumer 里没有任何一个订阅 `aats.system.audit_records`）。
这 28K 条消息在 stream 里纯属"等 7 天 TTL 到期"。

### 1.3 根因链

1. `audit.py:438` — audit service 每次 handler 处理完 publish 一条 `AUDIT_RECORDS` 到 NATS（每个 decision cycle ~5-10 条）
2. `nats_bus.py:1128 js.publish()` 把消息送入 `AATS_EVENTS` stream
3. **同一 publish 调用里** `nats_bus.py:1095 event_store.append(envelope)` 把 envelope 落 PG（持久化）
4. NATS stream 里的消息 **没有任何 consumer 订阅**（除了本来无 AUDIT_RECORDS consumer 外，其他 strategy.* 等虽然有 consumer 但 consumer 已秒级 ack，消息其实可 remove）
5. `retention=LIMITS` + `max_age=7 天` 让消息在 stream 里滞留 7 天
6. audit_records 60KB 级大 payload × 28K 条 / 7 天 → 稳定填满 4GB stream
7. 接近 `max_bytes` 时每次 publish 触发 server 端 **old-message discard compaction**（JetStream 同步 I/O）
8. publish 等 ack 时等的是这个 compaction → nats-py 抛 `TimeoutError("nats: timeout")`
9. audit handler 内部的 `await publish_model(AUDIT_RECORDS, ...)` 抛异常被 `resilient_subscription_handler` 捕获 → `noncritical_subscription_failed` 日志

### 1.4 根本设计错误

**NATS JetStream stream 被误当成"长期存档"用了**。系统实际架构是双层：

- **Postgres `event_store`（publish 路径的 `event_store.append`）**：长期存档，7 天+ 合规回放
- **NATS JetStream stream**：应该只是 hot buffer 给 live consumer 订阅

关键验证：`aats/services/reconciliation_service/replay.py:146, 626, 639` 等 `ReplayEngine` 全部走 `self.event_store.*`（PG），**不依赖 NATS stream 保留**。

所以 `max_age=7 天` 配置**没有任何业务需求支撑**，只是保险过度。

---

## 2. 目标（可测）

| 指标 | 现状 | 目标 |
|---|---|---|
| `AATS_EVENTS` stream size | 4.00 GiB（99.985% 上限）| **<1 GiB**（~23% 上限，留 77% headroom）|
| `AATS_EVENTS` msgs | 69,834 | 稳态 ~10,000（按 1/7 保留比例推算）|
| `noncritical_subscription_failed error=nats: timeout` 频率 | 每 2-3 分钟 1 条 | **清零或 <1/小时** |
| Stream compaction 触发频率 | 每次 publish 接近 max_bytes 时触发 | **永不触发**（始终远低于 max_bytes）|
| `max_bytes`（不动）| 4 GiB | 4 GiB（headroom 从 0.015% 回到 77%）|
| 合规/回放能力 | event_store 7 天+（不依赖 NATS）| 不变 |

---

## 3. 改动清单（共 3 代码 + 3 测试）

### 3.1 `aats/bus/nats_bus.py` 两处

**L487-494** 默认 spec：
```python
 DEFAULT_AATS_EVENTS_SPEC = StreamSpec(
     name="AATS_EVENTS",
     topics=DEFAULT_CRITICAL_EVENTS_TOPICS,
-    max_age_seconds=604_800,             # 7 天（合规 / 回放窗口）
+    max_age_seconds=86_400,              # 1 天（hot buffer — ReplayEngine 走
+                                         # event_store/PG 做合规/回放，NATS 不再
+                                         # 承担长期存档）
     max_bytes=4_294_967_296,             # 4 GB
     max_msgs=5_000_000,                  # 500 万条
     max_msg_size=4_194_304,              # 4 MB（与 MARKET 对称）
 )
```

**L576** `NatsBusConfig` 默认值：
```python
-    stream_max_age_seconds: float = 7 * 24 * 60 * 60
+    stream_max_age_seconds: float = 24 * 60 * 60
```

### 3.2 `aats/bootstrap/settings.py` 一处

**L197-200**：
```python
-    nats_stream_max_age_seconds: int = Field(
-        default=7 * 24 * 60 * 60,
-        description="JetStream retention max age (seconds). Default 7 days.",
-    )
+    nats_stream_max_age_seconds: int = Field(
+        default=24 * 60 * 60,
+        description=(
+            "JetStream retention max age (seconds). Default 1 day — NATS stream "
+            "is a hot buffer; long-term replay/audit is served from PG event_store."
+        ),
+    )
```

### 3.3 三处测试同步

**`tests/unit/test_event_bus_backend_settings.py:51`**：
```python
-def test_default_nats_stream_max_age_is_seven_days() -> None:
-    """默认 retention 7 天，与 NatsBusConfig 一致。"""
+def test_default_nats_stream_max_age_is_one_day() -> None:
+    """默认 retention 1 天 — NATS stream 是 hot buffer，不承担长期存档
+    （长期合规/回放由 PG event_store 承担，见
+    docs/task/aats_events_stream_retention_root_fix_sow.md）。"""
     settings = AATSSettings.model_validate({})
-    assert settings.nats_stream_max_age_seconds == 7 * 24 * 60 * 60
+    assert settings.nats_stream_max_age_seconds == 24 * 60 * 60
```

**`tests/unit/test_nats_bus_skeleton.py:412-417`**：同样改名从 `_is_seven_days` → `_is_one_day`，断言 `86_400`。

**`tests/unit/test_nats_bus_skeleton.py:1284`** 注释：里面的 `604_800` 如果是示例 mock 值（不是默认值断言），保留（见 §3.4 spot-check）。

### 3.4 不改的地方（spot-check 过源码）

- **`test_nats_stream_migrate.py:90, 343`**：migrate 测试里的 `max_age=604_800.0` 是**老配置 mock**（测试 "从 7 天配置 drift 到新配置" 的迁移逻辑），保留作为老配置回放场景。新 spec drift detection 会识别出差异并触发 update。
- **`test_hot_state_store.py:338/343/348`**：这些是 Redis TTL 测试，与 NATS stream 无关，保留。

### 3.5 不改 `max_bytes`

保持 4 GiB。理由：
- 1 天窗口下预估 stream size ~600 MB，4 GiB 有 85% headroom 足够处理突发（比如 consumer 掉线 6 小时的缓冲）
- 改 `max_bytes` 要同步改 nats-server.conf 的 `max_file_store`，复杂度高，不必要

---

## 4. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 / 证据 |
|---|---|---|---|
| consumer offline >24h 导致消息永久丢失 | 低 | 中 | 生产所有主进程（gateway/decision/execution/market）docker restart 秒级；WSL2 整机 outage 历史上未超 1h；event_store 从 PG 完整保留，ReplayEngine 路径不受影响 |
| stream drift update 失败（老 stream 已存在 7d 配置）| 低 | 低 | `nats_bus.py:379` 有 drift 检测 + `AddOrUpdate` 逻辑，NATS JetStream 支持 live 改 max_age；deploy 后老消息会在 1d 边界自然 expire |
| 迁移期间 old messages 一次性 discard 占用 IO 高峰 | 低 | 低 | NATS JetStream expire 是增量操作（按时间切块），不是一次全删；观测期 1 小时内确认 bytes 下降 |
| audit publish 仍有 nats: timeout（根因不止 stream 满）| 中 | 中 | 本 SOW 完成后观察 30 分钟，若 timeout 仍超 1/小时，启动 follow-up 方案 A（§6） |

---

## 5. 验证计划

### 5.1 deploy 后 10 分钟内

```bash
# 确认 stream max_age 已应用（字段单位纳秒）
curl -s http://127.0.0.1:8222/jsz?streams=true | \
  python3 -c "import json,sys;d=json.load(sys.stdin);[print(s['name'],s['config'].get('max_age')) for acc in d['account_details'] for s in acc['stream_detail']]"
# 预期 AATS_EVENTS max_age=86400000000000 (1d in nanoseconds)
```

### 5.2 deploy 后 2 小时

- stream `bytes` 应降到 ~3 GiB（6/7 是 >1 天老的数据，一小时内 evict）
- `noncritical_subscription_failed error=nats: timeout` 应清零

### 5.3 deploy 后 24 小时

- stream `bytes` 稳态 <1 GiB
- stream `msgs` 稳态 ~10,000
- 无 compaction 相关 log
- `replay` 测试（触发一次 operator-driven reconciliation validate + replay）仍正常——证明 event_store 确实承担了 replay 职责

---

## 6. 本 SOW 不覆盖（follow-up）

| # | 主题 | 原因 |
|---|---|---|
| 1 | **AUDIT_RECORDS 完全不走 NATS**（方案 A）| 更彻底的根治——audit_records 有 0 live consumer，连进 stream 都是浪费。但要改 publish 路径的分流逻辑（`nats_bus.py:publish_envelope` 判断"无 consumer subscribe 的 topic 跳过 js.publish 只 event_store.append"），改动大、测试覆盖面大、有蝴蝶效应。本 SOW 的 max_age=1d 已解决 99% 症状，方案 A 作为 follow-up 评估收益后再决定 |
| 2 | StreamSpec `max_bytes` 调优 | 1d 窗口下 4 GiB 已远超所需，无必要动 |
| 3 | 拆 stream（按 topic 分 retention）| 复杂度高；本 SOW 1d 全局收敛已满足所有 topic 的 hot buffer 需求 |
| 4 | nats-server.conf `max_file_store` 调优 | 不相关——本 SOW 不扩容 |

---

## 7. 签收条件

- [ ] 3 处代码改动 + 3 处测试改动全部合入 main
- [ ] 单元测试 `test_default_nats_stream_max_age_is_one_day` 等全绿
- [ ] 相关回归测试（`test_nats_bus_skeleton` / `test_event_bus_backend_settings` / `test_nats_stream_migrate`）全绿
- [ ] 部署后 2 小时 stream `bytes < 3 GiB`
- [ ] 部署后 24 小时 stream `bytes < 1 GiB`
- [ ] 部署后 24 小时 `noncritical_subscription_failed error=nats: timeout` 频率 < 1/小时

---

## 8. 审批记录

| 角色 | 姓名 | 日期 | 意见 |
|---|---|---|---|
| 起草 | Claude | 2026-04-20 | 初稿，基于源码直接证据（nats_bus.py / replay.py / 运行时查询）|
| 审核 | @excellentang | — | — |
| 实施 | — | — | — |
