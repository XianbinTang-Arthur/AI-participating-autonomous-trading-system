# Stage 6 Slice 6.3 热修：portfolio_repo → cache 同步 listener

## 1 背景

Slice 6.3 引入 `PortfolioSnapshotCache` 作为 `_latest_scoped_snapshot`
的热路径。D9 决策是"production 路径绕过 cache 直接打 PG，write 端由
outbox publisher 显式 publish 到 cache"。

这个设计对 **outbox publisher 这一条路径** 成立，但 repo 还有一系列
绕过 outbox publisher 直接写的路径：

```
aats/services/execution_engine/recovery.py:179        → save_snapshot(healed)
aats/services/execution_engine/recovery.py:215        → save_snapshot(rebuilt)
aats/services/reconciliation_service/repair.py:108    → save_snapshot(rebuilt)
aats/services/reconciliation_service/repair.py:248    → save_snapshot(repaired)
aats/services/projections/ledger_portfolio.py:81      → save_snapshot(ledger projection)
aats/services/projections/ledger_portfolio.py:146     → save_snapshot(ledger projection)
aats/services/portfolio_service/positions.py:470      → save_snapshot(legacy positions)
aats/services/portfolio_service/positions.py:524      → save_snapshot(legacy positions)
```

以及所有直接调 `runtime.portfolio_repo.save_snapshot(...)` 的测试。

这些路径 commit 到 repo，但 **不更新 cache**。
`OperatorQueryService._latest_scoped_snapshot` 查 cache 先于 repo：

```python
cache = getattr(self.runtime, "portfolio_snapshot_cache", None)
if cache is not None:
    cached = cache.get_sync(self.state_scope)
    if cached is not None:
        return cached              # ← 读到 stale cache
return latest_snapshot_for_scope(self.runtime.portfolio_repo, self.state_scope)
```

`build_runtime` 启动时写了一份 `snapshot_origin='runtime_bootstrap'` 的
空 snapshot 到 cache 和 repo。于是 recovery / repair 之后 cache 里永远
是那份空 bootstrap snapshot，dashboard 看到的持仓永远是 0。

这是 **资金安全 bug**：operator 紧急情况下看到的持仓是旧的，Stage 9
dryrun 前必须修。

### 1.1 触发条件

`test_positions_endpoint_exposes_dual_leg_instrument_state_for_derivatives_snapshot`
直接调 `runtime.portfolio_repo.save_snapshot(...)` 注入 2 条对冲腿，然后
调 `/positions`。失败原因：cache 命中 bootstrap 的空 snapshot → row
的 `dual_legged=False`。

在 clean HEAD（无本 fix，无 Slice 6.4 改动）上也稳定复现，证明与 Slice
6.4 无关。

## 2 目标与非目标

### 目标

- **I1-fix**：同一进程内，任何 `portfolio_repo.save_snapshot(...)` 后
  读 `portfolio_snapshot_cache.get_sync(scope)` 必须看到同一份 snapshot
  （无需 NATS round-trip 或 Redis hydrate）。
- 测试回归：`test_positions_endpoint_exposes_dual_leg_instrument_state
  _for_derivatives_snapshot` pass。
- 不破坏 Slice 6.3 D5：outbox publisher 的 Redis + NATS 广播逻辑保持不变。
- 不破坏 Slice 6.3 D6：`_apply_locally` 的 `snapshot_ts <= existing` 去重
  仍然有效（listener 写 → outbox publisher 写 → 第二次写因 ts 相等被
  noop）。

### 非目标

- **不** 解决跨进程 stale 问题：recovery / repair 路径写 snapshot 后其他
  3 个进程的 cache 仍然需要等 outbox publisher 的 NATS 路径或下次
  bootstrap 从 Redis hydrate。本 fix 只保证 **write 那一侧的进程内一致**。
  跨进程把 recovery / repair 收敛到 outbox publisher 统一入口是 Stage 6
  后续 slice 的事。
- 不修改 `save_snapshot_in_session`（outbox publisher 用的 session 版本）：
  那条路径已经显式调 `cache.publish()`，无需 listener。
- 不 wrap `PortfolioRepository` 成 decorator：Slice 6.3 D9 明确禁止。本
  fix 只给实现类加一个可选 listener 字段，协议接口不变。

## 3 决策

### D1 用 listener 钩子而非 repo wrapper
- 在 `InMemoryPortfolioRepository` / `PostgresPortfolioRepository` 两个
  实现类里加 `_snapshot_listener: Callable[[PortfolioSnapshot], None] | None`
  字段 + `attach_snapshot_listener(listener)` 方法。
- `save_snapshot` 写完 repo 后立即调用 listener（如已注入）。
- 理由：最小侵入，协议 `PortfolioRepository` 不变，不违反 D9。

### D2 listener 是 sync 的（不是 async）
- 理由：`save_snapshot` 本身是 sync 方法，sync → async 需要 loop 调度，
  两种实现（尤其 `asyncio.to_thread(self.portfolio_repo.save_snapshot, ...)`
  这种 worker thread 路径）无法稳定调度 async listener。
- 代价：listener 内部不能 await Redis 写；**但这是可接受的** —— Redis
  端由 outbox publisher 路径覆盖，listener 只负责**同进程 in-memory dict
  即时同步**。

### D3 新增 `PortfolioSnapshotCache.apply_sync(snapshot)`
- 纯 sync 方法：`scope_fingerprint = _scope_fingerprint_from_snapshot(snapshot);
  self._apply_locally(fingerprint, snapshot)`。
- 不写 Redis、不发 NATS。复用现有的 `_apply_locally` idempotent 规则。
- 作为 listener 的 thin adapter：`portfolio_repo.attach_snapshot_listener(cache.apply_sync)`。

### D4 listener 注入点：`build_runtime` 创建 cache 之后立即 attach
- 具体位置：`config.py:3798` 之后的几行，紧挨着 `portfolio_snapshot_cache
  _initialized` log。
- 时机选择：必须在 `portfolio_snapshot_cache.bootstrap(...)` 之后（此时
  `_latest` 已 hydrate 完），任何后续 `portfolio_repo.save_snapshot(...)`
  都能命中 listener。

### D5 失败隔离：listener 抛出不能拖垮 save_snapshot
- listener 调用包 try/except，异常只 log warning，save_snapshot 正常返回。
- 理由：save_snapshot 是关键路径，不能因为 cache 路径炸掉而回滚 repo 写。

### D6 outbox publisher 路径不变
- `PostgresPortfolioOutboxPublisher._persist_fill_projection_sync` 用的是
  `save_snapshot_in_session`，**不经过 listener 钩子**。
- 它的 cache 写入继续走外部的 `await self._publish_to_cache(snapshot)`。
- 理由：session 事务未 commit 前不能通知 cache（cache 会看到未 commit 数据），
  所以只能在 session.commit() 之后由外部显式触发。

### D7 测试：用 InMemoryPortfolioRepository + InMemoryHotStateStore 单测
- unit test 位于 `tests/unit/test_portfolio_snapshot_cache_listener.py`（新增）
- 覆盖：
  1. listener 未 attach：save_snapshot 不抛，cache miss
  2. listener attached：save_snapshot 后 cache.get_sync 立即返回同一 snapshot
  3. idempotent：同一 snapshot 重复 save，cache 只更新一次（fingerprint ts 比较）
  4. listener 抛异常：save_snapshot 仍然完成，repo.history() 有新 entry
- 另外 `test_positions_endpoint_exposes_dual_leg_instrument_state_for
  _derivatives_snapshot` 应该自然 pass（本 fix 的主目标）

## 4 API 改动

### 4.1 `InMemoryPortfolioRepository`
```python
class InMemoryPortfolioRepository:
    def __init__(self) -> None:
        self._snapshots: list[PortfolioSnapshot] = []
        self._snapshot_listener: Callable[[PortfolioSnapshot], None] | None = None

    def attach_snapshot_listener(
        self, listener: Callable[[PortfolioSnapshot], None]
    ) -> None:
        self._snapshot_listener = listener

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        self._snapshots.append(snapshot)
        self._notify_listener(snapshot)

    def _notify_listener(self, snapshot: PortfolioSnapshot) -> None:
        listener = self._snapshot_listener
        if listener is None:
            return
        try:
            listener(snapshot)
        except Exception:
            # best-effort：cache 通知失败不能拖垮 repo 写
            pass  # 真实实现 log warning
```

### 4.2 `PostgresPortfolioRepository`
```python
class PostgresPortfolioRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self._snapshot_listener: Callable[[PortfolioSnapshot], None] | None = None

    def attach_snapshot_listener(self, listener) -> None:
        self._snapshot_listener = listener

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        with self.session_factory() as session:
            self.save_snapshot_in_session(session, snapshot)
            session.commit()
        # commit 成功后才通知 listener，避免未 commit 数据污染 cache
        self._notify_listener(snapshot)
```

注意：`save_snapshot_in_session` 不调 listener，因为 session 尚未 commit。
outbox publisher 调 `save_snapshot_in_session` 之后自己在 commit 后用
`await self._publish_to_cache(snapshot)` 触发 cache 更新，行为不变。

### 4.3 `PortfolioSnapshotCache`
```python
def apply_sync(self, snapshot: PortfolioSnapshot) -> None:
    """Sync version of publish() without Redis/NATS writes.

    For listener wiring from portfolio_repo.save_snapshot. Only updates
    the local dict idempotently; cross-process propagation remains the
    outbox publisher's responsibility.
    """
    scope_fingerprint = self._scope_fingerprint_from_snapshot(snapshot)
    self._apply_locally(scope_fingerprint, snapshot)
```

### 4.4 `build_runtime` 注入
在 `config.py:3798` 的 `PortfolioSnapshotCache(...)` 构造后 +
`bootstrap(...)` 完成后立即：

```python
storage.portfolio_repo.attach_snapshot_listener(
    slices.portfolio_snapshot_cache.apply_sync
)
log_event(
    get_logger("aats.bootstrap"),
    "portfolio_repo_cache_listener_attached",
    process_role=effective_process_role or "monolith",
)
```

## 5 风险 & 回滚

### 风险

| # | 风险 | 缓解 |
|---|------|------|
| R1 | listener 循环（listener 内调 save_snapshot）| 设计约束：`apply_sync` 不碰 repo，物理上无环 |
| R2 | 多线程写 repo，listener 竞争 `_latest` dict | `_apply_locally` 是同步 dict 赋值，GIL 保证原子；乱序时 ts 比较处理 |
| R3 | outbox publisher 与 listener 双写同一 snapshot | `_apply_locally` 的 `snapshot_ts <= existing` 规则让第二次写 noop |
| R4 | listener 注入时机早于 cache bootstrap | attach 在 `cache.bootstrap()` 之后调；且 apply_sync 不依赖 `_bootstrapped` 标志 |
| R5 | 跨进程 stale：recovery 进程更新 repo 但其他进程 cache 仍旧 | 超出本 fix scope，由下一个 slice 把 recovery/repair 搬到 outbox publisher 下 |

### 回滚

单 commit，影响面：
- `aats/storage/portfolio_repo.py`（+1 字段 +1 方法，save_snapshot +1 行）
- `aats/storage/portfolio_repo_postgres.py`（同上）
- `aats/services/portfolio_service/snapshot_cache.py`（+1 方法 `apply_sync`）
- `aats/bootstrap/config.py`（+3 行注入）
- `tests/unit/test_portfolio_snapshot_cache_listener.py`（新增）

`git revert <commit>` 即可回滚。

## 6 验收

1. `test_positions_endpoint_exposes_dual_leg_instrument_state_for
   _derivatives_snapshot` pass
2. `tests/unit/test_portfolio_snapshot_cache_listener.py` 全部 pass
3. `tests/unit/` 全套回归 pass（1296+ 测试）
4. `tests/integration/test_operator_api.py` 全套 pass
5. `tests/integration/test_recovery.py` pass（recovery.py 的两处 save_snapshot
   现在会触发 cache listener）
