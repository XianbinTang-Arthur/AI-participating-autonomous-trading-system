# Gateway 慢查询系统化治理 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **文档状态**：待审批（2026-04-20 起草）
> **上游文档**：
> - 事故复盘：[`docs/review/orphan_fill_misclassification_root_cause_2026_04_20.md`](../review/orphan_fill_misclassification_root_cause_2026_04_20.md)
> - 后台调查报告：会话 JSONL transcript `a38c3cfb1c5d22e92`（核心结论已内联到本文）
> **工期估计**：2–3 天（5 个 Stage 分批 commit，每个 Stage 独立 revertable）
> **覆盖范围**：Agent 报告中方案 1+2+3+4 全套 + Part 4 `SINGLEFLIGHT_WAIT` 配套

---

## 1. 背景 & 目标

### 1.1 为什么治

事故复盘期间观察到 gateway `aats.operator_api.parallel` 频繁打 `parallel_fetch_slow` WARNING：

- wall=**79.1s** top5=[`blockers=79.082s, mode_snapshot=25.907s, recovery=25.882s, snapshot=15.787s, execution=7.131s`]
- wall=**45.3s** top5=[`decision_context_events=45.113s, reconciliation_refs=20.896s, snapshot_events=1.750s`]
- wall=**38.6s** top5=[`guarded_live_run_packet=38.448s, guarded_live_preflight=28.933s`]

前端 [`aats/api/static/modules/api-client.js:14`](../../aats/api/static/modules/api-client.js) 有 `DEFAULT_TIMEOUT_MS = 30_000`。任何 wall > 30s 的后端调用会让浏览器抛 DOMException，展示成 **"signal is aborted without reason"** 红色 banner（本次事故中阻断了操作员多次 resume 点击）。

### 1.2 目标（可测）

| 指标 | 当前（P95） | 目标（本 SOW 完成后 P95） |
|---|---|---|
| `/dashboard/bundle` 主 panel 组 wall | 30–80s | **<10s** |
| `/dashboard/bundle` 整体 wall（含 deferred） | 45s+ | **<20s** |
| `/system/resume` 端到端 | 38–88s | **<15s** |
| `decision_context_events` 单路 | 45s | **<100ms** |
| `blockers` 单路 | 79s | **<3s** |
| `guarded_live_preflight` wall | 29s | **<6s** |

前端 `DEFAULT_TIMEOUT_MS` **保持 30s** 作为性能红线，不放宽（agent 明确建议，避免后端慢变成长期默认）。

---

## 2. 现状拆解（来自 agent 实测 + 本次 spot-check）

### 2.1 量化

- `event_store` 热表 **545,379 行 / 6.2 GB**；`strategy.decision_context` topic 占 12,549 行 / 15 MB payload
- `event_store_archive` 空；最早热行 2026-04-17，**housekeeping 14 天阈值实质从未触发**
- 单路 `by_topic_scoped(DECISION_CONTEXTS, scope)`：
  - PG EXPLAIN ANALYZE 390 ms
  - psycopg `fetchall()` 2.2 s
  - SQLAlchemy ORM 2.2 s
  - **8 路并发 wall=19 s / 12 路并发 wall=29.8 s**（GIL + psycopg jsonb 反序列化 + PG work_mem sort buffer 争用）

### 2.2 根因（按权重）

| 权重 | 根因 | 证据 |
|---|---|---|
| 🔴 最大 | 12 路 parallel_fetch 里多个"一次拉 12.5K 行 event_store"的 query 同时跑 → 单路从 2.2s 膨胀到 18–30s | metrics 几路 `by_topic_scoped(..., limit=None)` |
| 🔴 最大 | `_build_metrics` 里 `decision_context_events / order_intent_events / snapshot_events / reconciliation_refs` 全量拉 list 后只用 `len()` 或 id-set | `query_service.py:7945-7966` |
| 🟡 次要 | `_parallel.py:82-87` 嵌套守卫直接串行化 → `build_system_mode` / `build_recovery_view` 内部 9 路并发退化为 9 路串行 | `_parallel.py:82` |
| 🟡 次要 | `guarded_live_preflight` / `run_packet` 的 7-8 个 TTL 方法纯串行调用 | `query_service.py:1666-1673, 2010-2018` |
| 🟢 附带 | `_SINGLEFLIGHT_WAIT_SECONDS=60` > 前端 `DEFAULT_TIMEOUT_MS=30` 导致 follower 永远撑不到 leader 完成 | `query_service.py:194` / `api-client.js:14` |

### 2.3 不是根因（验证过）

- `reconciliation_reports` / `reconciliation_findings` 数据量：5255 / 2476 行，不大
- `_SHARED_MAX_WORKERS=12` 本身够用（问题在于单路并发互拖）
- Postgres 索引：复合索引存在，Bitmap Scan 热数据命中 shared buffer

---

## 3. 总体策略

按"改动最小 + 收益最大 + 可独立回滚"拆成 5 个 Stage，顺序执行：

| Stage | 内容 | 工时 | 独立收益 | 依赖 |
|---|---|---|---|---|
| **S1** | `_build_metrics` count/ids-only 化 | 0.5 天 | `decision_context_events 45s→<50ms` | 无 |
| **S2** | metrics 内部并发改串行（配合 S1 做零风险验证） | 0.25 天 | 避免 S1 的 count 查询误并发劣化 | S1 |
| **S3** | `guarded_live_preflight` / `run_packet` 串行改 parallel_fetch | 0.5 天 | `preflight 29s→6-8s` | 需配合 S4 解嵌套 |
| **S4** | `_parallel.py` 嵌套守卫从"全串行降级"改为"本地小线程池" + DB pool 调整 | 0.75 天 | `recovery_view 18s→3-4s` | 无 |
| **S5** | `_SINGLEFLIGHT_WAIT_SECONDS` 60→25；前端 `DEFAULT_TIMEOUT_MS` 保持 30 | 0.25 天 | 消除 follower 孤儿 | S1-S4 稳定后 |

每个 Stage 单独一个 commit + 独立回滚（Stage 间无代码耦合，只有"越早改越稳"的观测关系）。

---

## 4. 详细设计

### S1 — `_build_metrics` count/ids-only 化

#### 4.S1.1 现状代码

[`aats/services/operator/query_service.py:7940-7977`](../../aats/services/operator/query_service.py)：

```python
phase1_queries = {
    # ...
    "order_intent_events": lambda: list(
        self.runtime.event_store.by_topic_scoped(topics.ORDER_INTENTS, scope=self.state_scope)
    ),
    "decision_context_events": lambda: list(
        self.runtime.event_store.by_topic_scoped(topics.DECISION_CONTEXTS, scope=self.state_scope)
    ),
    "snapshot_events": lambda: list(
        self.runtime.event_store.by_topic_scoped(topics.PORTFOLIO_SNAPSHOTS, scope=self.state_scope)
    ),
    "reconciliation_refs": lambda: {
        report.portfolio_snapshot_ref
        for report in self.runtime.reconciliation_repo.history_for_scope(scope=self.state_scope)
    },
    # ...
}
r = parallel_fetch(phase1_queries)
```

#### 4.S1.2 下游审查（硬性前置）

动手前必须 grep 出全部下游用法，确认它们**真的只要 count/ids**，不要 payload：

```bash
rg -n "r\[\"order_intent_events\"\]|r\[\"decision_context_events\"\]|r\[\"snapshot_events\"\]|r\[\"reconciliation_refs\"\]" aats/services/operator/
```

对每个匹配点，确认：
- `len(r["..."])` → 可以用 count
- `{e.event_id for e in r["..."]}` → 可以用 ids-only
- `for e in r["..."]: use e.payload` → ⚠️ **需要 payload，不能改**，此路保留全量拉取

**此步骤产物**：Phase-1 下游用法审查表，附在 commit message 里。

#### 4.S1.3 新增 API

**文件**：[`aats/storage/event_store_postgres.py`](../../aats/storage/event_store_postgres.py)

```python
def count_by_topic_scoped(self, topic: str, *, scope: StateScope) -> int:
    """返回指定 topic + scope 的事件数。用于 metrics 聚合。"""
    with self._session_factory() as session:
        return session.execute(
            select(func.count()).select_from(EventStoreRow)
            .where(EventStoreRow.topic == topic)
            .where(*_scope_where(scope))
        ).scalar_one()

def event_ids_by_topic_scoped(self, topic: str, *, scope: StateScope) -> list[str]:
    """只返回 event_id 列表，不反序列化 payload。用于 metrics id-set 聚合。"""
    with self._session_factory() as session:
        return list(session.execute(
            select(EventStoreRow.event_id)
            .where(EventStoreRow.topic == topic)
            .where(*_scope_where(scope))
        ).scalars())
```

**文件**：[`aats/storage/reconciliation_repo_postgres.py`](../../aats/storage/reconciliation_repo_postgres.py)

```python
def portfolio_snapshot_refs_for_scope(self, *, scope: StateScope) -> set[str]:
    """只返回 portfolio_snapshot_ref 的去重集合，不反序列化 payload。"""
    with self._session_factory() as session:
        return set(session.execute(
            select(ReconciliationReportRow.portfolio_snapshot_ref)
            .where(*_reconciliation_scope_where(scope))
            .where(ReconciliationReportRow.portfolio_snapshot_ref.isnot(None))
        ).scalars())
```

同样修改抽象基类 `aats/storage/event_store.py` 和 `aats/storage/reconciliation_repo.py` 加 `@abstractmethod`，然后在内存实现 `*_memory.py` 里补上简单的 list comprehension 对应版本。

#### 4.S1.4 改 `_build_metrics`

```python
phase1_queries = {
    # ...
    "order_intent_event_count":
        lambda: self.runtime.event_store.count_by_topic_scoped(
            topics.ORDER_INTENTS, scope=self.state_scope
        ),
    "decision_context_event_count":
        lambda: self.runtime.event_store.count_by_topic_scoped(
            topics.DECISION_CONTEXTS, scope=self.state_scope
        ),
    "snapshot_event_ids":
        lambda: self.runtime.event_store.event_ids_by_topic_scoped(
            topics.PORTFOLIO_SNAPSHOTS, scope=self.state_scope
        ),
    "reconciliation_refs":
        lambda: self.runtime.reconciliation_repo.portfolio_snapshot_refs_for_scope(
            scope=self.state_scope
        ),
    # ...
}
```

下游调用点同步改（按 §4.S1.2 的审查表逐个改）。

#### 4.S1.5 单元测试

新增 `tests/unit/test_event_store_postgres_count_api.py`（也可加在现有测试里），覆盖：
- count 对空 scope 返回 0
- count 与 `len(by_topic_scoped(...))` 结果一致
- `event_ids_by_topic_scoped` 返回去重/保序（要确认当前 by_topic_scoped 的保序契约是什么）

---

### S2 — metrics 内部并发改串行（S1 之后）

#### 4.S2.1 为什么做

S1 把四路从 15MB-full-scan 降到 count / id-set 后，它们变得极轻（每路 <100ms），**继续并发跑对 DB 的 shared buffer 争用没收益但开销存在**（线程上下文切换 + connection 获取）。

agent 报告 part 2 明确验证：并发 12 路共享 event_store 热表的 buffer 路径时，单路时间从 2.2s 膨胀到 18-30s。**count 查询由于不拉 payload，不会触发这个放大，但也没必要并发**。

#### 4.S2.2 改动

保持 `_build_metrics` 的 `parallel_fetch` 结构，但把 event_store 四路从 `phase1_queries` dict 挪出来，改成在 parallel 之前**串行**执行：

```python
# 串行执行 4 路 count/ids（每路 <100ms，合计 < 400ms）
metrics_counts = {
    "order_intent_event_count": self.runtime.event_store.count_by_topic_scoped(
        topics.ORDER_INTENTS, scope=self.state_scope
    ),
    "decision_context_event_count": self.runtime.event_store.count_by_topic_scoped(
        topics.DECISION_CONTEXTS, scope=self.state_scope
    ),
    "snapshot_event_ids": self.runtime.event_store.event_ids_by_topic_scoped(
        topics.PORTFOLIO_SNAPSHOTS, scope=self.state_scope
    ),
    "reconciliation_refs": self.runtime.reconciliation_repo.portfolio_snapshot_refs_for_scope(
        scope=self.state_scope
    ),
}

# 剩余并发 fan-out（snapshot / fills / phase1_shadow / rejections / open_orders / ...）
phase1_queries = {
    "snapshot": self._latest_scoped_snapshot,
    # ... 无 event_store 全扫的项
}
r = parallel_fetch(phase1_queries)
r.update(metrics_counts)
```

#### 4.S2.3 选项：不做 S2

如果 S1 后测出 "并发 4 count 查询也只 400ms"，保留并发更符合代码直觉（不额外引入局部串行块）。**S2 是 defense-in-depth**，视 S1 落地后的 metrics 实测决定。**建议：先不 merge S2，观察 S1 生产表现 24h 后再定**。

---

### S3 — `guarded_live_preflight` / `run_packet` 并行化

#### 4.S3.1 现状

[`query_service.py:1666-1672`](../../aats/services/operator/query_service.py)：

```python
mode_snapshot = self.system_mode()
recovery = self.recovery_view()
blockers = self.blockers()
account = self.account_state()
margin_buffer = self.margin_buffer_risk()
live_guard = self.derivatives_live_guard()
trial_guard = self.trial_guard()
account_snapshot = self.runtime.account_service.latest_snapshot()
```

[`query_service.py:2010-2018`](../../aats/services/operator/query_service.py)：

```python
preflight = self.guarded_live_preflight()
live_guard = self.derivatives_live_guard()
trial_guard = self.trial_guard()
margin_buffer = self.margin_buffer_risk()
recovery = self.recovery_view()
blockers = self.blockers()
positions = self.positions()
account = self.account_state()
forward_validation = self.forward_validation_report(window_days=7, period_count=4)
```

两段都是**纯串行**。每个 `self.xxx()` 内部已有 `_cached_ttl` 单飞缓存，冷启动时才会真正下穿到 DB，串行时互相等。

#### 4.S3.2 改动

```python
# _build_guarded_live_preflight
r = parallel_fetch({
    "mode_snapshot": self.system_mode,
    "recovery": self.recovery_view,
    "blockers": self.blockers,
    "account": self.account_state,
    "margin_buffer": self.margin_buffer_risk,
    "live_guard": self.derivatives_live_guard,
    "trial_guard": self.trial_guard,
    "account_snapshot": lambda: self.runtime.account_service.latest_snapshot(),
})
mode_snapshot = r["mode_snapshot"]
recovery = r["recovery"]
# ... 等等
```

#### 4.S3.3 关键约束：必须配合 S4

**S3 单独做有性能陷阱**：`_build_guarded_live_preflight` 从一个 panel 的 build 方法里调 `parallel_fetch`，会被 `_nesting_guard` 判定为嵌套，降级为串行，**一点收益都没有**。

所以 **S3 的 merge 必须在 S4 merge 之后**，或者把 S3 + S4 捆一个 commit。推荐：独立 commit，但**提交顺序严格 S4 → S3**。

#### 4.S3.4 其他散点并行化

同一文件里其他串行块（可选批量处理）：

```bash
rg -n "^\s+\w+ = self\.(system_mode|recovery_view|blockers|account_state|margin_buffer_risk|derivatives_live_guard|trial_guard)\(\)$" aats/services/operator/query_service.py
```

找到后统一用 `parallel_fetch` 包一下。但**不强制本 SOW 范围**，主要收益来自 preflight + run_packet 这两段。

---

### S4 — `_parallel.py` 嵌套守卫重写

#### 4.S4.1 现状

[`aats/services/operator/_parallel.py:82-87`](../../aats/services/operator/_parallel.py)：

```python
if getattr(_nesting_guard, "active", False):
    _logger.debug("parallel_fetch_nested_serial queries=%d", len(callables))
    return {name: fn() for name, fn in callables.items()}
```

当前设计本意：防止共享 12 worker 池内自己等自己饿死。但副作用：`build_system_mode` 里调 `recovery_view`（9 路并发）被全部降级为串行，18s 没法改善。

#### 4.S4.2 改动

改为嵌套时使用 **本地独立小线程池**（不占共享池 slot，不互相饥饿）：

```python
import contextlib
from concurrent.futures import ThreadPoolExecutor

_INNER_MAX_WORKERS = 4  # 嵌套层最多 4 个 worker，配合 DB pool 上限

def parallel_fetch(callables: dict[str, Callable[[], Any]], *, max_workers: int = 10) -> dict[str, Any]:
    if not callables:
        return {}
    if len(callables) == 1:
        name, fn = next(iter(callables.items()))
        return {name: fn()}

    if getattr(_nesting_guard, "active", False):
        # 嵌套层：用本地小池，避免占共享池 slot
        _logger.debug("parallel_fetch_nested_local queries=%d", len(callables))
        inner_workers = min(_INNER_MAX_WORKERS, len(callables))
        with ThreadPoolExecutor(
            max_workers=inner_workers,
            thread_name_prefix="parallel_fetch_inner",
        ) as inner:
            return _execute_with_executor(inner, callables, nested=True)

    # 外层：走共享池（原逻辑）
    executor = _get_shared_executor()
    return _execute_with_executor(executor, callables, nested=False)
```

把现有 executor + timed + drain 逻辑抽成 `_execute_with_executor(executor, callables, *, nested)`，外层和内层共用。

**注意 nesting guard 的语义**：内层 worker 线程里再嵌套再嵌套（3 层）会无限制展开，需要在 `_execute_with_executor` 里保留 `_nesting_guard.active = True` 设置，但增加一个"当前是否已是内层"的标记避免第 3 层再创建局部池（第 3 层退化为串行即可，3 层嵌套在生产代码里应该不存在）。

#### 4.S4.3 DB pool 配套扩容

[`aats/storage/session.py:198`](../../aats/storage/session.py) 当前：

```python
pool_size=10
max_overflow=20
# 合计 30 并发连接
```

改后线程总数上限：
- 共享池 12
- 每个共享池 worker 可能开一个 4-worker 局部池（只有嵌套 query 会开）
- 极端：12 + 12×4 = **60 个线程同时要连接**

DB pool 扩容：

```python
pool_size=15
max_overflow=45
# 合计 60 并发连接
```

**前置检查**：`deploy/wsl2-dev/postgres/postgresql.conf`（如果有）或 `docker compose` 的 Postgres 启动参数里 `max_connections` 至少要 ≥ 80（60 + 预留给 decision / execution / rdp-daemon 各自的连接池）。agent 报告未验证当前 `max_connections`，**必须在 S4 实施前先确认**。

```bash
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "SHOW max_connections"
```

#### 4.S4.4 S4 回滚策略

S4 改动是本 SOW 最高风险项（线程 + DB pool 同时改）。一旦生产观察到连接耗尽或 deadlock：

```bash
git revert <S4-commit-sha>
bash scripts/deploy.sh --skip-commit
```

单独 revert S4 不会影响 S1/S2/S3。

---

### S5 — 前端超时配套

#### 4.S5.1 改 `_SINGLEFLIGHT_WAIT_SECONDS`

[`aats/services/operator/query_service.py:194`](../../aats/services/operator/query_service.py)（事故修复时被改成 60）：

```python
_SINGLEFLIGHT_WAIT_SECONDS = 25  # 小于前端 DEFAULT_TIMEOUT_MS=30s
```

**理由**：follower 等 leader 最多 25s，仍在前端 30s 超时窗口内。leader 如果真的要 > 25s，follower 放弃等待自己下穿（会触发一次独立查询，代价是 DB 层少量重复工作；但比 follower 孤儿 + 前端 abort 好）。

#### 4.S5.2 前端 `DEFAULT_TIMEOUT_MS` 保持 30s

**不改**。[`aats/api/static/modules/api-client.js:14`](../../aats/api/static/modules/api-client.js) 注释里已有历史说明："任何'允许 loading 转圈 1 分钟再失败'的需求要走 options.timeout 显式覆盖，不要回退这里的默认值"。本 SOW 尊重这条红线。

#### 4.S5.3 新增 alert（可选）

在 `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml` 里加一条 Prometheus 告警：

```yaml
- uid: perf-parallel-fetch-slow-regression
  title: parallel_fetch_slow P95 回升
  condition: parallel_fetch P95 wall > 10s 持续 10 分钟
  severity: warning
```

（具体 PromQL 等实际指标采集点确定后再填；Loki query `count_over_time({job="aats"} |= "parallel_fetch_slow" [5m])` 可以先当基线）

---

## 5. 下游审查清单（S1 必读）

Stage 1 改 `_build_metrics` 的 4 个 key 语义（从 `list[Event]` 改为 `int` / `list[str]` / `set[str]`）。所有读取这 4 个 key 的下游必须同步改。

**必检位置**（命令 + 人工过一遍）：

```bash
cd aats/services/operator
rg -n 'metrics\["order_intent_events"\]|r\["order_intent_events"\]'
rg -n 'metrics\["decision_context_events"\]|r\["decision_context_events"\]'
rg -n 'metrics\["snapshot_events"\]|r\["snapshot_events"\]'
rg -n 'metrics\["reconciliation_refs"\]|r\["reconciliation_refs"\]'
```

**每个命中点写进审查表**：

| 位置 | 当前用法 | 能否迁 count/ids | 迁移后代码 |
|---|---|---|---|
| query_service.py:NNNN | `len(metrics["..."])` | 可（直接用 count） | `metrics["...event_count"]` |
| query_service.py:NNNN | `{e.event_id for e in metrics["..."]}` | 可（直接用 ids） | `set(metrics["..._ids"])` |
| ... | `for e in metrics["..."]: use e.payload` | ⚠️ 不能，保留全量拉取 | — |

表格入 S1 commit message。如果有"不能迁"的路径，S1 必须保留一个全量拉取 fallback（可能保留原 `decision_context_events` key 与新 `decision_context_event_count` 并存；冷路径全量拉，热路径用 count）。

---

## 6. 验证计划

### 6.1 每个 Stage 合入后的硬性验证

每个 Stage commit → push 到 WSL2 → deploy → 跑下面三步：

#### ① 单路查询时延（单元测试 / 手工脚本）

```python
# tests/perf/test_gateway_query_perf.py (新增)
def test_decision_context_count_under_100ms(operator_query_service):
    start = time.monotonic()
    operator_query_service.runtime.event_store.count_by_topic_scoped(
        topics.DECISION_CONTEXTS, scope=DERIVATIVES_CROSS_SCOPE,
    )
    assert time.monotonic() - start < 0.1
```

#### ② 端到端 bundle wall time（手工 + 脚本）

```bash
# 冷启动后 5 次 + 热缓存后 5 次，取 P95
for i in 1 2 3 4 5; do
  curl -k -sS -w 'wall=%{time_total}s\n' -b /tmp/cookies.txt \
    "https://localhost:8011/dashboard/bundle?view=risk&..."
  sleep 0.5
done
```

#### ③ `parallel_fetch_slow` 日志观察（Loki）

```
{job="aats"} |= "parallel_fetch_slow" | line_format "{{.wall}}s"
```

Stage 合入后 30 分钟内如果新出 `wall > 15s` 事件 → 立即 revert 该 Stage。

### 6.2 全部 Stage 合入后的回归基线

采集 24 小时稳定运行数据，和 §1.2 的目标表逐项对照。不满足的指标开 follow-up issue。

---

## 7. 提交顺序（严格）

```
S1 → verify P95 → commit
     ↓ 24h 观察
     ↓ 如 S2 仍有必要：
S2 → verify → commit
     ↓
S4 → verify → commit     (S4 先于 S3，否则 S3 会被嵌套守卫降级)
     ↓
S3 → verify → commit
     ↓
S5 → verify → commit
```

**不允许合并 commit**。每个 Stage 独立 SHA，便于精准 revert。

---

## 8. 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| S1 下游审查漏一处"需要 payload"的调用，线上 AttributeError | 中 | 高（dashboard 炸）| 审查表入 commit；合入前跑 `pytest tests/unit/ -k operator` 全绿；灰度 30min |
| S4 嵌套本地池 + 共享池线程总数放大，DB `max_connections` 耗尽 | 中 | 高（全系统 500）| 提前 `SHOW max_connections`；pool_size 调整随 S4 同 commit；合入后立即 `pg_stat_activity` 监控 |
| S3 合入早于 S4，parallel_fetch 被降级串行，看起来没事但收益消失 | 高 | 低（性能未改善而已）| 提交顺序严格 S4→S3；S3 验证包括 `parallel_fetch_slow` 的 wall 对比 |
| S5 `SINGLEFLIGHT_WAIT=25` 导致 follower 下穿 DB，冷启动 stampede 放大 | 低 | 中（冷启动 DB 负载 ×N）| 先观察 S1-S4 稳定后再合入 S5；如出现 stampede 把 `_SINGLEFLIGHT_WAIT` 回滚到 35s（介于前端 30 + 缓冲） |
| event_store 未来继续增长（当前 6.2GB），根治还要分区（agent 方案 5） | 高 | 中（半年内再次变慢） | **不在本 SOW 范围**；本 SOW 完成后开 follow-up SOW `event_store_partitioning_and_archive_policy_sow.md` |
| S1 的 `count_by_topic_scoped` / `event_ids_by_topic_scoped` 在内存实现里语义走样（测试时用 Memory，生产用 Postgres） | 低 | 中（测试绿但生产挂）| 单元测试同时跑 Memory 和 Postgres 两种实现（testcontainers） |

---

## 9. 回滚预案

### 9.1 单 Stage 回滚

```bash
# 比如 S4 出事
git revert <S4-commit-sha>
bash scripts/deploy.sh --skip-commit
# 观察 15 分钟
```

### 9.2 全量回滚

5 个 Stage 全部 revert：按 S5 → S3 → S4 → S2 → S1 倒序 revert（保持 merge 顺序反过来）。

### 9.3 紧急止血

如果运行中发现严重 DB 卡顿且不能立即 revert：临时把 `_SHARED_MAX_WORKERS` 从 12 降到 4 / `_INNER_MAX_WORKERS` 从 4 降到 1，`docker restart aats-gateway`。

---

## 10. 本 SOW 不覆盖的工作（follow-up）

| # | 主题 | 原因 |
|---|---|---|
| 1 | `event_store` range 分区（按 `event_timestamp` 每日/每周）+ housekeeping 从 14d→3d | 需 schema 迁移 + outbox/replay 兼容测试，独立 SOW |
| 2 | `historic_orphan_fill` finding_type 改名为 `local_fill_outside_exchange_lookback_window` | 已在复盘文档 §L1 提到，独立 issue 跟踪 |
| 3 | `_build_metrics` 下游真正需要 `event.payload` 的路径改走专用端点（而不是把 payload 都塞进 dashboard bundle） | S1 审查时如果发现这类路径超过 2 处，拆独立 SOW |
| 4 | 前端主 bundle 自动降级（单 panel 失败不影响其他 panel 展示）| 独立 UX 改动，和本 SOW 无关 |

---

## 11. 签收条件

本 SOW 完成的定义：

- [x] S1 S2 S3 S4 S5 全部 commit 合入 main
- [x] §1.2 目标表 6 项指标全部达标（24h 稳定样本）
- [x] 事故复盘文档 [`orphan_fill_misclassification_root_cause_2026_04_20.md`](../review/orphan_fill_misclassification_root_cause_2026_04_20.md) 的 §"未收尾事项"第 1 条（gateway 慢查询根因治理）划掉
- [x] 新增 perf 单元/集成测试至少 3 份
- [x] follow-up SOW（event_store 分区）草稿至少存在

---

## 12. 审批记录

| 角色 | 姓名 | 日期 | 意见 |
|---|---|---|---|
| 起草 | Claude | 2026-04-20 | 初稿 |
| 审核 | @excellentang | 2026-04-20 | "你看着办"——全权委托推进 |
| 实施 | Claude | 2026-04-20 | S1 实施中 |

---

## 13. 实施记录

### 2026-04-20 "看着办"委托后的四点决策

基于 `SHOW max_connections=200` 和 4 个下游消费点 grep 审查的实测：

1. **不加 S0** —— PG max_connections=200（当前 36 活跃连接），S4 pool 扩到 60 无压力
2. **S1 改 3 路而不是 4 路** —— 下游审查发现 `snapshot_events` 有 `event.payload.get("source_fill_id")` 的 payload 消费路径（`query_service.py:7988-7992`），不能 count/ids 化；保留全量拉取。独立 follow-up：给 `event_store` 加 `source_fill_ids_by_topic_scoped` 或专用 snapshot 字段索引
3. **S2 降级为"条件触发"** —— S1 落地后 24h 观察 count 并发是否仍有劣化；无劣化则跳过 S2，完成定义不强制 S2
4. **event_store 分区继续 follow-up** —— §10 条 1 不变

### S1 实际审查表

| 变量 | 原用法 | 新用法 | 改名后 key |
|---|---|---|---|
| `order_intent_events` | `len(r["..."])` 单一用法 | `count_by_topic_scoped()` | `order_intent_event_count` |
| `decision_context_events` | `len(r["..."])` 单一用法 | `count_by_topic_scoped()` | `decision_context_event_count` |
| `snapshot_events` | ① `event.payload.get("source_fill_id")` ② `len(...)` ③ `event.event_id for event in ...` | **不变**（保留全量拉） | 不变 |
| `reconciliation_refs` | `if event.event_id not in refs` | `portfolio_snapshot_refs_for_scope()` | 不变（本来就是 set） |

### S1 文件清单（本次改动）

- `aats/storage/base.py`：`EventStore` 加 `count_by_topic_scoped` Protocol，`ReconciliationRepository` 加 `portfolio_snapshot_refs_for_scope` Protocol
- `aats/storage/event_store.py`：`InMemoryEventStore.count_by_topic_scoped` 实现
- `aats/storage/event_store_postgres.py`：`PostgresEventStore.count_by_topic_scoped` 实现（hot + archive 合计）
- `aats/storage/reconciliation_repo.py`：`InMemoryReconciliationRepository.portfolio_snapshot_refs_for_scope` 实现
- `aats/storage/reconciliation_repo_postgres.py`：`PostgresReconciliationRepository.portfolio_snapshot_refs_for_scope` 实现（SQL DISTINCT）
- `aats/services/operator/query_service.py`：`_build_metrics` 迁移 3 路到新 API + key 改名 + 下游消费点同步
- `tests/unit/test_storage_count_and_refs_api.py`：**新增** 9 条契约测试（全绿）

### S1 测试结果

- `pytest tests/unit/test_storage_count_and_refs_api.py` → **9/9 passed**
- `pytest tests/unit/test_event_store_archive.py tests/unit/test_reconciliation.py tests/unit/test_reconciliation_repair.py` → **42/42 passed**（回归确认）

### S1 未完成项（续 S1.f 步）

- [ ] 等操作人批准 commit
- [ ] 部署到 WSL2 并观察 `parallel_fetch_slow` 日志 24h
- [ ] 基于观察决定 S2 是否需要执行
