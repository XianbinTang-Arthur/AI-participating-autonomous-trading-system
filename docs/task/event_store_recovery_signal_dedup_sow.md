# Event Store `recovery` 信号 98.5% dedup SOW

> **状态**：2026-04-21 autonomous 发现，**等用户审批**才落地。
> **作者**：Claude，autonomous 夜场迭代
> **触发**：`scripts/diag/event_store_bloat_audit.sh` 显示 98.53% dedup 潜力

## 1. 问题陈述

### 数据事实

| 指标 | 值 | 备注 |
|------|-----|------|
| event_store 总量 | 6.6 GB / 578k 行 | 4.4 天运行，1.65 GB/day |
| `GuardSignalUpdate` 事件 | 3.3 GB (50%) | 独占半边 |
| `recovery` 信号单条大小 | 149 KB avg | 709 KB 字段 `independent_recovery_snapshots` |
| `recovery` publish interval | 13.2s | 每小时 ~273 条 |
| 最近 1h unique payload | **4 种 / 273 条** | **98.5% 重复** |

### 根因

`aats/services/governance_engine/guard_signal_cache.py:212 publish()` 每次
调用都把 `snapshot` 发到 NATS topic `system.guard_signal_updates`。
`aats/bus/nats_bus.py:1218-1220` 在 publish 路径上**先** `event_store.append`。
所以每次 publish 必然落一条 PG 行，即使 snapshot 与上次完全相同。

`recovery` 信号的 `independent_recovery_snapshots` 包含系统所有 independent
strategy sleeve 的 recovery state（58 个 sleeve × ~12 KB = 709 KB），
在系统稳定运行时几乎不变。

### 影响

- **存储**：省 3.2 GB 历史 + 降增速 33× （1.65 GB/day → ~50 MB/day）
- **启动时间**：冷启动读 event_store 时间与 size 成正比
- **replay 时间**：replay_engine 遍历 event_store 时扫到大量重复记录
- **housekeeping 压力**：归档 job 每天要搬 1.65 GB 到 archive

## 2. 三档方案

### 方案 A：Publisher-side dedup（推荐，ROI 最高）

**改动**：`guard_signal_cache.py` 的 `publish()` 内加 payload hash 比较。

```python
# GuardSignalHotStateCache.__init__
self._last_published_hash: str | None = None

async def publish(self, snapshot: dict[str, Any]) -> None:
    now = time.time()
    enriched = {**snapshot, "_cached_at": now, ...}
    
    # 本地 / Redis 总是更新（不影响 freshness）
    self._latest = enriched
    self._last_updated_at = now
    # ... Redis set ...

    # 2026-04-21 dedup：payload 与上次相同 → 跳过 NATS + event_store 持久化
    # 仍然更新 local + Redis，保证新 subscriber bootstrap 时能读到最新值
    import hashlib, json
    payload_str = json.dumps(snapshot, sort_keys=True, default=str)
    payload_hash = hashlib.sha256(payload_str.encode()).hexdigest()
    if self._last_published_hash == payload_hash:
        return  # skip bus.publish
    
    # ... 原 NATS publish 路径（event_store.append 会在 nats_bus.py 里自动跑）...
    self._last_published_hash = payload_hash
```

**风险 / 对策**：

| 风险 | 对策 |
|------|------|
| 下游依赖"每 13s 一次"做 staleness 检测 | 检查消费者：decision 侧 `GuardSignalHotStateCache.snapshot()` 用 `time.time() - self._last_updated_at` 判定 stale，本地 `_last_updated_at` 每次 publish 都更新 —— 不受影响 |
| 下游通过 NATS 消费用 message count 做健康检查 | 检查：暂无此类消费者（grep `subscribe(GUARD_SIGNAL_UPDATES)` 只有 GuardSignalHotStateCache，它只关心 payload 内容不关心计数） |
| 新 subscriber bootstrap 丢失 | 本身有 Redis bootstrap 路径（`bootstrap()` 从 Redis key 读），dedup 不影响 |
| 网络丢包导致 NATS 消息未送达 | 原来也有这问题，heartbeat 降频后更糟一点；但 Redis TTL = `stale_threshold * 3 = 360s`，bootstrap 总能恢复 |
| `_cached_at` 时间戳变化导致 hash 每次都不同 | **关键**：hash 必须基于**业务 payload** 不包括 `_cached_at`。伪代码里用 `snapshot` 不是 `enriched`，正确 |

**验证步骤**：
1. unit test：构造同一 snapshot 连续 publish 两次，第二次应 skip bus.publish
2. unit test：snapshot 变化后应恢复 publish
3. integration test：跑 10 分钟、观察 event_store GuardSignalUpdate 条目数 —— 应降 98%+
4. 跑 `scripts/diag/event_store_bloat_audit.sh` 再次采样验证

**工作量**：~50 行代码 + 2 个单测 + 验证。预估 2 小时。

### 方案 B：Event type 拆分

**改动**：把 `GuardSignalUpdate` 拆成两种：
- `GuardSignalStatusUpdate`（booleans + 小字段）每 13s publish
- `GuardSignalDetailUpdate`（`independent_recovery_snapshots` 等大字段）
  on-change publish

**风险更高**：要改 publisher + 订阅者 + 消费者（UI query_service 等）。
不建议做 —— 除非未来还有其他类似 bloat 信号，再做 schema 级别拆分。

### 方案 C：不持久化（只走 NATS ephemeral）

**改动**：`nats_bus.py` 里把 `GUARD_SIGNAL_UPDATES` 从"持久 topic"列表中
移除。订阅者仍收到（NATS JetStream with TTL），但 event_store 不写。

**风险**：丢失完整审计追溯。recovery 信号的历史对事后故障分析有价值。
不建议。

## 3. 推荐

**方案 A**。单点改动 + 零语义变更 + 用户看不到任何差异（UI/风控/recovery
行为都和现在一样）。唯一差异：event_store 少了 98.5% 的重复记录。

## 4. 验收标准

部署后 1h 采样：
```bash
bash scripts/diag/event_store_bloat_audit.sh
```

应看到：
- `total_events` 仍然 ~273（publish 频率不变）→ ❌ 这是 skip 前的统计，看
  **event_store 里** `GuardSignalUpdate` 条目数应降到 ~4-10（= dedup 后实际
  写入次数）
- `unique_snapshot_hashes` ≈ `total_events`
- `dedup_savings_pct` ≈ 0%（因为所有 persist 的都已经是 unique）

同时 Grafana 上观察：
- event_store insert rate 下降 ~50%（GuardSignalUpdate 占 50% event_store，
  其中 recovery 占 99%+）
- event_store 总增速：从 1.65 GB/day 降到 ~50 MB/day

## 5. Rollback

单 commit revert 即可恢复旧行为。无数据迁移、无 schema 变更。

## 6. 下一步

**等用户审批本 SOW。** 审批后 Claude 或用户任一一方可以：
1. 在一个新分支 `fix/guard-signal-dedup` 上落地方案 A
2. 跑 unit test + integration test（WSL2）
3. Deploy + 1h 观察
4. Merge 到 main

审批形式建议：用户写 "LGTM 方案 A" 或 "改 X Y Z" 到 session log 或 commit
message 里。
