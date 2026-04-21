# 2026-04-21 夜场 · 延迟性 latent issue 审计报告

> **上下文**：用户心脏不适去休息，我独自迭代 8 小时。做完所有紧急修复后，
> 我用 Explore 代理做了一轮 "latent 风险" 系统扫描。此文档记录发现、
> 我是否行动、为什么。
>
> **用意**：用户醒来能快速看懂每一项，决定是否启动详细 SOW。

## 审计分档定义

- 🔴 **HIGH**：生产已经在走，数据可量化，ROI 清晰
- 🟡 **MED**：有理论风险，实际影响取决于未来运营规模
- 🟢 **LOW**：现象存在但有自愈机制或已有兜底

## 已在本场修复的（列在这里方便索引）

| 问题 | 修复 | commit |
|------|------|--------|
| `recovery_posture` history() 扫全表 → recovery_view 111s+ | → `latest_for_scope` | 5a6c383 |
| `health._base_components` history() 扫全表 | → 3-tier cascade | 7b30eaf |
| PG pool 耗尽（15+ idle-in-tx） | 加 `idle_in_transaction_session_timeout=60s` | afddc1a |
| 上面的安全网误杀 `advisory_lock` 连接 | 加 `connection.commit()` | 23c8e7e |
| `_cached` / `_cached_ttl` TOCTOU 惊群放大 | 把 cache 写 + event.set() 合并 | 288692c |
| `inference._decision_fills` 扫全表 fills 表 | 加 `fills_for_decisions` + SQL-side filter | 3dbe026 |

---

## 未修（已确诊、等用户审批）

### ⚠️ 重大：event_store `recovery` 信号 98.5% dedup 潜力

**现象**：
- event_store 总 6.6 GB，`GuardSignalUpdate` 占 50%（3.3 GB）
- `recovery` 信号单条 149 KB 平均、13s publish 一次
- 其中 `independent_recovery_snapshots` 字段占 709 KB（58 个 snapshot）
- 最近 1h 共 273 条，但 payload 哈希**只有 4 种** → 98.5% 是重复

**理论收益**：减 3.2 GB 历史存量 + 从 1.65 GB/day 增速降到 ~50 MB/day（33×）

**为什么不改**：`recovery` 是 RiskEngine only-reduce 决策依赖的 safety-critical
信号。铁律"绝不改 kill switch / recovery policy 语义字段"。

**建议方案**（需用户审批）：
1. ★ 首选：dedup at publish time — guard_signal_cache.py 加 payload hash 比较，
   相同则跳过 event_store 持久化（仍更新 Redis/NATS heartbeat）。风险：
   下游若依赖 "每 13s 一次" 做 staleness 检测会误触发。
2. 拆 event_type — `RecoveryStatusSummary`（booleans，小）每 13s，
   `RecoverySnapshotDetail`（大数组）on-change。风险：消费者要同时更新两种。
3. 不持久化，只走 NATS — 风险：丢审计追溯。

**诊断工具**：`scripts/diag/event_store_bloat_audit.sh`（已落地 048a1a8）

---

## 未修（理论风险、低优先、可等）

### 🟡 MED：`decision_engine/trigger.py` dict 无清理

- `_timeframe_locks`（line 261）和 `_consecutive_failures`（line 283-284）
  按 `(symbol, timeframe)` 累加 key，没有任何路径删除
- 当前系统只有 1 个 symbol（`BTC-USDT-SWAP`），累积最多 ~5 entries，负面影响为零
- **何时需要修**：规模扩张到 10+ symbols 且 allowed_symbols 会动态变化时

**建议方案**：在 `RuntimeSettings` 的 `allowed_symbols` reload 钩子上加一次
`_timeframe_locks` 清理；或引入 `cachetools.LRUCache(maxsize=100)`。

### 🟡 MED：后台 loops 没 jitter（潜在 thundering herd）

- `aats/bootstrap/config.py` 984/992/1052/1068/1078/1090/1103/1112/1131 行
  都是 `while True: await asyncio.sleep(fixed_interval)`
- 4 个 process 同时启动时会"锁步"命中 DB，引发 Prometheus 尖峰
- 实际生产观察：尚未见明显尖峰（系统小、流量低）

**建议方案**：把 `asyncio.sleep(interval)` 改为 `asyncio.sleep(interval +
random.uniform(0, interval * 0.1))`。

### 🟡 MED：`fill_event_cache._pending_evictions` list 无上限

- Redis 断线期间，evicted fill_id 会堆到 `_pending_evictions`
- 虽然 Redis 恢复后会清掉，但"恢复前"的高频 eviction 会让这个 list 吃内存

**建议方案**：`collections.deque(maxlen=100)`，满了自动丢最老。

### 🟡 MED：`okx_private_websocket.py:106` keepalive_task 可能静默死

- `asyncio.create_task(self._keepalive_loop(...))` 如果 raise，异常存在 task
  对象里但主循环不 await，直到外层 `finally` 才 cancel（此时已死）
- 自愈：下次 OKX server-side timeout → 连接关闭 → 主循环退出 → 重连
- 负面窗口：一次静默死意味着 30s-2min 的"无 keepalive"期

**建议方案**：主循环定期 `if keepalive_task.done(): raise from result` 检查。

### 🟢 LOW：`okx_websocket.py:176` `ping_timeout=None`

- 设计性决定（应用层 keepalive 代替 protocol-level），已有注释说明
- 如果应用层 keepalive 挂了，会失效 → 但有 #6 的重连链路兜底

**不改，保持设计意图。**

---

## 确认无问题（audit 代理误判）

### ✖ `okx_rest.py:221` `timeout=None` 风险

- Audit 代理说"如果 okx_timeout_seconds 是 None，httpx 会无超时"
- 实际：`settings.py:306 okx_timeout_seconds: float = 15.0`，pydantic 强制 float，
  不可能为 None
- **结论**：假阳，无需处理

### ✖ `query_service._cache` / `_ttl_cache` 无边界

- Audit 代理说"accumulates forever"
- 实际：`_invalidate_cache()` 在 query_service 里 15+ 处、在
  reconciliation_system_queries 里 4 处调用（所有 mutation action）
- 动态 cache key（如 `independent_version_summary:{ids}:...`）理论会生长，
  但只要 scope 固定，key 空间有限；scope 变时会被 `_scope_cache_fragment`
  清掉
- **结论**：设计健壮，无需处理

---

## 优先级小结（给用户的 1 分钟版）

**醒来优先看**（按价值排序）：
1. 🔥 **event_store recovery 信号 dedup**：3.2 GB 立减 + 增速 33× 降低。3 档方案任选。
2. 可选：decision_engine trigger.py 的两个 unbounded dicts，在扩 symbol 前修。
3. 其他 MED 项等规模增长时再修。

**autonomous 期间我没做的**（原因都是 safety-critical 或 invasive）：
- 上面所有"未修"项
- 任何碰到 order submission / order cancel / fund movement / risk limit 的代码

## 追溯

所有发现的原始 agent 报告、数据采样 SQL、相关文件行号都在 `docs/autonomous_sessions/2026_04_21_night.md` 的 T+0 到 T+5 记录里。
`scripts/diag/` 目录下有可重跑的验证工具。
