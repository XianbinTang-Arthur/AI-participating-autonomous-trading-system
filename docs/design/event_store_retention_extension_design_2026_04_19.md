# event_store 保留期扩展设计（2.5 天 → ≥14 天）

| 字段 | 值 |
|------|-----|
| 撰写日期 | 2026-04-19 |
| 作者 | Claude (worktree `eager-bose-f54f78`) |
| 触发背景 | `docs/review/signal_edge_scale_calibration_2026_04_19.md` step 1 — 16448 行 / 2.5 天样本使 score→realized_edge 回归统计显著性不足 |
| 使用场景 | 仅限内部 calibration / 回归分析（不涉及合规监管） |
| 推荐方案 | **方案 C（启用已有冷热分离）+ 方案 B（topic 分级保留期）** |
| 预估工作量 | 1 天（方案 C 最小可用） / 2–3 天（C+B 全量） |

---

## 1. 现状定位（file:line）

### 1.1 持久层

| 文件 | 关键位置 | 说明 |
|------|---------|------|
| `aats/storage/sqlalchemy_models.py:25-46` | `EventEnvelopeModel` / `event_store` 表 | 热表，有 `event_timestamp` 索引 |
| `aats/storage/sqlalchemy_models.py:49-72` | `EventEnvelopeArchiveModel` / `event_store_archive` 表 | **已存在**归档表，带 `ix_event_store_archive_timestamp` |
| `aats/storage/event_store_postgres.py:288-331` | `archive_before(before_ts=...)` | 把 hot 中早于 `before_ts` 的行**搬**进 archive，基于 `event_id` 去重 |
| `aats/storage/event_store_postgres.py:267-286` | `list_envelopes(...)` | **已经**对 hot+archive 做 UNION，查询方透明 |

### 1.2 后台任务

| 文件 | 位置 | 行为 |
|------|------|------|
| `aats/storage/housekeeping.py:52-81` | `purge_published_outbox` | outbox 已发布行 7 天后 DELETE |
| `aats/storage/housekeeping.py:98-126` | `purge_old_archive_events` | **归档表**超 90 天 DELETE |
| `aats/bootstrap/config.py:1102-1122` | `_housekeeping_loop` | 每 6 小时一次 `run_all`，只做上面两件事 |

**关键事实**：`housekeeping.run_all` **不清理 event_store 热表**。"2.5 天数据"不是被某个 cleanup 任务删出来的，而是"系统连续运行的时间就是 2.5 天"。只要系统不重启，热表会无限累积。

### 1.3 手动入口

- `scripts/archive_event_store.py` — CLI，支持 `--before-days N / --before-hours / --before-ts / --summary-only`；从未被自动调度（cron/compose 均无引用）。

### 1.4 Deploy 行为

- `scripts/deploy.sh:355` — `docker compose down --timeout 5`（**不带 `-v`**），postgres 卷跨 deploy 保留；event_store 数据在 deploy 后仍在。

### 1.5 归档表实际状态

| 表 | 行数 | 大小 |
|----|------|------|
| `event_store_archive` | **0** | 128 kB（空骨架） |

**结论**：冷热分离的架构已经就位（建表 + 读路径 UNION + 搬运 API + CLI），但**从未被执行过一次**。这是半成品状态。

---

## 2. 实测数据（2026-04-20 采样，系统连续运行 ~2.81 天）

### 2.1 基础盘

| 指标 | 值 |
|------|-----|
| `event_store` 行数 | **404 672** |
| 时间跨度 | 2026-04-17 04:47 → 2026-04-20 00:18 ≈ **2.81 天** |
| 平均速率 | **144 000 行/天** |
| `event_store` 总大小 | **4 486 MB**（heap 486 MB + indexes 168 MB + TOAST ≈ 3.83 GB） |
| `aats_live_derivatives` 总大小 | 5 694 MB（event_store 占 **79%**） |
| WSL2 根盘可用空间 | 913 GB / 1007 GB（5% used） |
| postgres_data docker volume | 6.22 GB |

磁盘完全不是瓶颈。

### 2.2 按 topic 的 payload 占用（TOP 10）

| topic | rows | payload | rows/天 | MB/天 | 价值等级 |
|-------|------|---------|---------|-------|---------|
| `system.guard_signal_updates` | 52 969 | **2131 MB** | 18 850 | **758** | 🟥 低（调试信号流） |
| `account.snapshots` | 13 253 | 945 MB | 4 716 | 336 | 🟥 低 |
| `strategy.coordinator_snapshots` | 9 368 | 308 MB | 3 333 | 110 | 🟨 中 |
| `system.audit_records` | 140 877 | 165 MB | 50 135 | 59 | 🟨 中（合规审计流） |
| `strategy.sleeve_intents` | 65 576 | 139 MB | 23 335 | 49 | 🟩 高（allocator 溯源） |
| `strategy.portfolio_allocation_decisions` | 9 368 | 118 MB | 3 333 | 42 | 🟩 **高（calibration）** |
| `strategy.position_target` | 9 368 | 57 MB | 3 333 | 20 | 🟩 高 |
| `strategy.decision_outcome` | 9 383 | 28 MB | 3 339 | 10 | 🟩 **高（calibration）** |
| `strategy.profile_optimization_reports` | 3 229 | 17 MB | 1 149 | 6 | 🟨 中 |
| `strategy.profile_evaluations` | 19 374 | 19 MB | 6 894 | 7 | 🟨 中 |
| `strategy.decision_context` | 9 393 | 11 MB | 3 342 | 4 | 🟩 **高（calibration）** |
| `strategy.baseline_assessment` | 9 393 | 11 MB | 3 342 | 4 | 🟩 **高（calibration）** |

**4 个 calibration 核心 topic**：`baseline_assessment + portfolio_allocation_decisions + decision_outcome + decision_context` — 合计约 **168 MB / 2.81 天 ≈ 60 MB/天**。

---

## 3. 扩展到 14 / 30 天的资源成本外推

### 3.1 不做任何分级（相当于"让系统连续运行更久"）

| 保留期 | 行数 | 体积（按现实 11 KB/行 平均） |
|--------|------|---------------------------|
| **14 天** | ~2.0 M | **~22 GB** |
| **30 天** | ~4.3 M | **~47 GB** |

WSL2 盘完全容得下，但：
- `ix_event_store_topic_symbol_seq` 等 B-tree 索引会在 2M+ 行规模下开始影响写入延迟（每写一行要维护 2 个 composite index + 7 个普通 index）
- `list_envelopes(start_at=...)` 全表扫老数据会变慢（但带 `event_timestamp` 索引缓解）
- TOAST 表（大 payload 存储）会持续膨胀，VACUUM 成本上升

### 3.2 启用 topic 分级保留（方案 B）

若对低价值 topic（guard_signal_updates / account.snapshots）只保留 3 天：

| 保留策略 | 14 天占用 | 30 天占用 |
|---------|----------|----------|
| 所有 topic 保留全部 | 22 GB | 47 GB |
| 低价值 3 天 + 其他 14 天 | **~10 GB** | — |
| 低价值 3 天 + 其他 30 天 | — | **~17 GB** |

其中最大节约来自 `system.guard_signal_updates`（758 MB/day）。只把它的保留期压到 3 天，30 天场景就能从 47 GB 压到 ~25 GB。

---

## 4. 候选方案对比

| 方案 | 侵入度 | 工作量 | 长期可维护性 | 回滚难度 | 是否利用已有实现 |
|------|-------|-------|-------------|---------|---------------|
| **A. 延长清理阈值** | — | — | — | — | ⚠️ **不适用**：没有清理阈值可调 |
| **B. 按 topic 分级保留** | 中 | 2–3 天 | ⭐⭐⭐⭐ | 易（关闭分级逻辑即可） | 需扩 housekeeping + archive_before 增加 topic 过滤 |
| **C. 启用冷热分离归档** | 低 | 1 天 | ⭐⭐⭐ | 极易（关后台任务即止） | ⭐ **完全复用**已有代码 |
| **D. Postgres 表分区** | 高 | 5–7 天 + 停机窗口 | ⭐⭐⭐⭐⭐ | 难（迁回非分区表耗时） | 部分（复用 schema，索引要重建） |

### 4.1 方案 A — 延长清理阈值

**不适用**。当前系统里 `event_store` 热表**根本没有**自动清理任务。所谓 2.5 天并不是被哪个 `retention_days` 限制住的，而是系统运行时长。要延长"清理阈值"就等于从零写一个清理任务——不如直接走方案 C（用现成的 archive）或 D（分区）。

### 4.2 方案 B — 按 topic 分级保留

**核心思路**：高价值 topic 保留长（30 天），低价值 topic 保留短（3–7 天）。配合 C 或 D 才能真正起效（本身只是一种策略，不落地需要另一个机制去执行）。

**落地改动**：
- `aats/storage/housekeeping.py` 增加 `archive_hot_by_topic(topic_retention: dict[str, int])` 方法
- `aats/storage/event_store_postgres.py` 增加 `archive_before_by_topic(topic, before_ts)` 重载
- `aats/bootstrap/config.py` housekeeping_loop 调用新方法，从配置读取 retention 策略
- `configs/housekeeping_retention.yml` 新配置文件，列出每个 topic 的保留天数

**风险**：
- calibration 查询依赖 `list_envelopes` 走 UNION，如果低价值 topic 老数据被归档到 archive 表，UNION 仍能返回，但要确保 calibration 逻辑读 archive
- topic 列表会演进，需要一个"默认保留天数"（比如 14 天）兜底未列出的 topic

### 4.3 方案 C — 启用冷热分离归档（推荐基础）

**核心思路**：利用已有的 `event_store_archive` + `archive_before` + CLI，启用它们。

**最小实现（1 个增量）**：
在 `housekeeping.py:DatabaseHousekeeping` 中新增一个方法，把 hot 中早于 N 天的行整批 archive：

```python
def archive_hot_event_store(
    self,
    *,
    older_than_days: int = 14,
    batch_size: int = 5000,
) -> int:
    """把 event_store 中早于 older_than_days 的行批量搬到 event_store_archive。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    total_archived = 0
    with self._session_factory() as session:
        while True:
            # 1. select batch
            batch = session.execute(
                select(EventEnvelopeModel)
                .where(EventEnvelopeModel.event_timestamp < cutoff)
                .order_by(EventEnvelopeModel.sequence_id)
                .limit(batch_size)
            ).scalars().all()
            if not batch:
                break
            # 2. insert archive rows (ON CONFLICT DO NOTHING for idempotency)
            archive_rows = [_to_archive_row(r) for r in batch]
            session.bulk_save_objects(archive_rows)
            # 3. delete hot rows
            ids = [r.sequence_id for r in batch]
            session.execute(
                delete(EventEnvelopeModel)
                .where(EventEnvelopeModel.sequence_id.in_(ids))
            )
            session.commit()
            total_archived += len(batch)
    return total_archived
```

然后在 `run_all` 里加一行调用。

**要点**：
- `archive_before` 当前实现（`event_store_postgres.py:288`）是 **N+1 查询**（逐行 `session.scalar(...)` 检查存在），不适合批量。新方法应该用 `ON CONFLICT (event_id) DO NOTHING` 的批量 INSERT 替代。
- `list_envelopes` 已经 UNION archive + hot（`event_store_postgres.py:267-286`），calibration 查询完全透明，不需要改调用方。
- 归档表自身的老化由 `purge_old_archive_events(older_than_days=90)` 兜底，已存在。

**风险**（很低）：
- 单个 batch 事务不宜过大（5k 行 / ~60 MB 以下，避免 WAL 膨胀）
- 第一次跑会搬 400k 行，约 5–10 分钟，单次 CPU 尖峰；建议首次手动跑 `archive_event_store.py --before-days 1`

### 4.4 方案 D — Postgres 表分区

**核心思路**：`event_store` 改为 PARTITION BY RANGE (event_timestamp)，按天分区。老分区 `DROP PARTITION` 瞬秒，不产生 TOAST bloat。

**工作量**：
- `event_store` 重建为分区父表（需要**停机窗口**，用 `pg_partman` 或手写触发器）
- 4 个索引全部改成 partition-local
- 唯一约束 `event_id` 要变为全局 `CREATE UNIQUE INDEX ON event_store (event_id, event_timestamp)` 或靠 writer 保证
- Alembic 迁移脚本
- 跨分区 `sequence_id` PK 要改成 `(sequence_id, event_timestamp)` composite

**收益**：
- 查询走 partition pruning，`event_timestamp` 范围查询 O(1) 找到分区
- DROP 老分区不触发 VACUUM
- 索引单 partition 维度，写入成本稳定

**劣势**：
- 工作量大（一周）
- 现有 404k 行数据要做 online migration（`pg_repack` 或停机 dump/restore）
- 风险面大：任何 migrate 错误会丢数据

**结论**：D 是 6 个月后的事。当前数据量（2.8 天 400k 行）完全撑得住简单归档。

---

## 5. 推荐方案

### 5.1 分期落地

| 阶段 | 内容 | 何时 | 工作量 |
|------|------|------|-------|
| **Phase 0（立刻/手动）** | 跑 `archive_event_store.py --before-days 1 --profile derivatives_live` 验证归档管线；确认 calibration 查询仍能覆盖 archive 数据 | P1-A 主线不打断，运维时间窗口跑一次 | 15 min |
| **Phase 1 ✅ 已完成（2026-04-19）** | **方案 C** — 在 `housekeeping.py` 增加 `archive_hot_event_store(older_than_days=14)`，加入 `run_all`，启用 housekeeping_loop 自动搬运 | P1-A 落地后、下一轮 calibration 前 | 1 天（含 unit test + WSL2 smoke） |
| **Phase 2（可选）** | **方案 B** — 引入 topic 分级 retention；低价值 topic（`guard_signal_updates` / `account.snapshots` / `system.audit_records`）3 天归档，其余 14/30 天 | 如果 Phase 1 后发现磁盘增速仍不可接受 | 2 天 |
| **Phase 3（远期）** | **方案 D** — 当 event_store 行数稳定在千万级时启用分区 | 6+ 个月后 | 1 周 + 停机窗口 |

### Phase 1 交付物（worktree `agent-a052ed34`）

- `aats/storage/housekeeping.py` — 新增 `ArchiveReport` dataclass + `DatabaseHousekeeping.archive_hot_event_store`, 并把它接入 `run_all`
- `aats/bootstrap/config.py::_housekeeping_loop` — log_event 新增 `archive_hot_*` 字段
- `scripts/maintenance/backfill_event_store_archive.py` — 幂等 backfill CLI（dry-run + --confirm 双层保护）
- `tests/unit/test_event_store_archive_housekeeping.py` — 12 个单元测试（happy/batch/idempotent/cutoff/rollback/dry_run/max_batches/as_dict）
- `docs/operations/event_store_archive_runbook.md` — 运维手册
- `docs/review/path_b_event_store_archive_phase1_2026_04_19.md` — 完工报告

### Phase 1 实测（2026-04-19 22:00 WSL2 采样）

| 指标 | 值 |
|------|-----|
| event_store 当前行数 | 411,365 |
| 时间跨度 | 2.87 天（2026-04-17 04:47 → 2026-04-20 01:23）|
| `older_than_days=14` 待归档 | **0 行** — 系统连续运行时长 < 14 天 |
| `older_than_days=1`（压测用） | 244,752 行 / 2.38 GB / 25 批 |

**说明**：Phase 1 部署后，在系统实际运行满 14 天前（约 2026-05-01），
`archive_hot_event_store` 每次 tick 都是 NOOP。这是**预期行为**——
与老数据共存 14 天后才会有搬运发生。

### 5.2 推荐理由

1. **复用最大化**：`event_store_archive` + `archive_before` + `list_envelopes` UNION 已经齐备，只差一个定时调度入口。
2. **风险最低**：归档是幂等的（`event_id` 唯一约束），最差情况重复搬运会 NOOP；出问题关后台任务即止。
3. **满足 calibration 需求**：只要系统连续运行 ≥14 天，热表就有 ≥14 天数据可用；Phase 1 落地后，老数据即使被归档，calibration 仍能通过 UNION 读到。
4. **压力测试路径清晰**：Phase 2 的 topic 分级是"看需要再做"的优化，不强求。
5. **不阻塞 P1-A**：Phase 0 是纯运维动作；Phase 1 是独立 PR，与 P1-A 的 allocator / 双通道 momentum 改动无交集。

### 5.3 验证 / 回滚路径

| 指标 | 健康阈值 | 来源 |
|------|---------|------|
| `event_store` 热表行数 | < 3 M | `SELECT COUNT(*) FROM event_store` |
| `event_store` 热表最旧时间戳 | ≥ `now() - retention` | `SELECT MIN(event_timestamp) FROM event_store` |
| housekeeping 每次搬运耗时 | < 30 s | log `db_housekeeping_completed` 字段 |
| archive 表增长率 | 与 hot 搬出量一致 | `archive_summary()` |

**回滚**：注释掉 `_housekeeping_loop` 中对 `archive_hot_event_store` 的调用即可。已归档的数据仍可通过 `list_envelopes` UNION 读到，无数据丢失。

---

## 6. 立即决定 vs 等主任务空闲

**可以等主任务空闲**。理由：

1. **当前并不阻塞**：系统实际是 400k 行 / 2.81 天 / 4.5 GB，再跑 11 天就有 14 天数据 —— calibration 可以推迟到 2026-05-01 左右再做，那时天然有 ≥14 天样本。
2. **磁盘无压力**：913 GB 可用，哪怕 30 天累积到 47 GB 也只占 5%。
3. **唯一风险**：如果 P1-A 期间因 bug/崩溃重启服务 + `docker compose down -v` 清卷（运维误操作），数据会从零重来。但 deploy.sh 不带 `-v`，此风险 = 手动运维误操作概率。

**应该尽快做的事**（今天/明天就能起 PR）：
- Phase 0（运维）：手动跑一次 `archive_event_store.py --summary-only` 验证 CLI 能跑（无数据影响）。
- Phase 1（编码）：准备好 PR，与 P1-A 合并后一起部署。

---

## 7. 迁移脚本 outline

已起草 `scripts/maintenance/event_store_retention_migration_archive.py`（见附录文件）。核心流程：

1. `--dry-run`：输出将要归档的 event_id 分布（按 topic 统计 + 最旧/最新 event_timestamp），**不改数据**。
2. `--apply --before-days N`：分批（5000 行/事务）把 hot 中早于 N 天的行搬到 archive；已有 event_id 跳过（幂等）。
3. 每批打印进度；中断后可重跑，从最早的"未归档热行"继续。
4. 退出码：
   - 0 = 成功完成
   - 2 = dry-run 完成
   - 3 = DB 不可达或归档表缺失

**使用顺序**：
```
# 1. 先看一眼要搬多少
python scripts/maintenance/event_store_retention_migration_archive.py \
    --profile derivatives_live --before-days 14 --dry-run

# 2. 实际搬运
python scripts/maintenance/event_store_retention_migration_archive.py \
    --profile derivatives_live --before-days 14 --apply

# 3. 验证
python scripts/archive_event_store.py --profile derivatives_live --summary-only
```

---

## 8. Open Questions / TODO

- [ ] P1-A 落地后，核对 calibration 查询（`docs/review/signal_edge_scale_calibration_2026_04_19.md` 的 SQL）是否真的走 `list_envelopes` 路径。如果 calibration 直接 `SELECT * FROM event_store`，则需要改成 UNION archive，否则方案 C 落地后 calibration 读不到老数据。
- [ ] 评估一次真实 archive 的事务时长 / WAL 峰值（测一次 1 天数据 ~150k 行）。
- [ ] housekeeping_loop 间隔当前是 6h；归档引入后是否需要每 1h 跑一次以让热表平稳？目前看 6h 足够（每次 ~900k 行归档以下）。
