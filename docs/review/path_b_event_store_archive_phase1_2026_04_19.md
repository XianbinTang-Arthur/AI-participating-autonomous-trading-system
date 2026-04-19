# Path B — event_store 归档 Phase 1 完工报告

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


| 字段 | 值 |
|------|-----|
| 日期 | 2026-04-19 |
| Worktree | `agent-a052ed34` |
| 设计依据 | `docs/design/event_store_retention_extension_design_2026_04_19.md` |
| 状态 | ✅ 代码 + 测试 + 文档完成，**未 deploy、未 push** |

---

## 1. 执行概览

实施了设计文档 Phase 1（方案 C — 启用冷热分离归档），把空骨架的
`event_store_archive` 表真正接入自动调度。完成范围：

1. `archive_hot_event_store(older_than_days=14)` 函数
2. 每 6 小时自动调度（通过现有 `_housekeeping_loop`）
3. 冷启动 backfill CLI（幂等 + 双层保护）
4. 12 个单元测试
5. 运维 runbook

**严格遵守**任务约束：不 deploy、不 push、不改 event_store 写入逻辑、
任何 DELETE 必有先 INSERT、Phase 1 范围不碰 index/partition。

---

## 2. 做了什么

### 2.1 代码改动

| 文件 | 类型 | 说明 |
|------|------|------|
| `aats/storage/housekeeping.py` | 新增 | `ArchiveReport` dataclass + `DatabaseHousekeeping.archive_hot_event_store`；`run_all` 增加 `archive_hot_report` 字段 |
| `aats/bootstrap/config.py::_housekeeping_loop` | 修改 | 日志 event 追加 `archive_hot_copied/deleted/batches/time_ms` 四个字段 |
| `scripts/maintenance/backfill_event_store_archive.py` | 新增 | 一次性冷启动脚本，`--dry-run` / `--apply --confirm` 双模式 |
| `tests/unit/test_event_store_archive_housekeeping.py` | 新增 | 12 个单元测试 |
| `docs/operations/event_store_archive_runbook.md` | 新增 | 运维手册 |
| `docs/design/event_store_retention_extension_design_2026_04_19.md` | 修改 | Phase 1 标"已完成"+交付物列表 |
| `docs/review/path_b_event_store_archive_phase1_2026_04_19.md` | 新增 | 本报告 |

### 2.2 `archive_hot_event_store` 设计要点

- **函数签名**（任务要求）：
  ```python
  def archive_hot_event_store(
      self,
      *,
      older_than_days: int = 14,
      batch_size: int = 10_000,
      dry_run: bool = False,
      max_batches: int | None = None,
  ) -> ArchiveReport
  ```
- **事务安全**：每个 batch 独立 session，先 `INSERT ... SELECT ...`
  到 archive（逻辑方言无关：先 SELECT existing event_ids → 过滤 →
  `INSERT` 全新行）再 DELETE 对应 sequence_id；任一步失败 → rollback，
  hot 表完整无损。
- **幂等**：archive.event_id 有 UNIQUE 约束，但我们不依赖 Postgres
  专用 `ON CONFLICT`，而是业务层过滤，SQLite/Postgres 双方言通用。
- **批量**：默认 10k 行/批。`max_batches` 参数让 housekeeping_loop
  每次 tick 有上限（默认 6 批 = 60k 行），避免单 tick 占用过长。
- **返回报告**：`ArchiveReport(copied_rows, deleted_rows, batches,
  time_taken_ms, oldest_ts_before, oldest_ts_after, cutoff_ts,
  dry_run)` — 涵盖任务要求的全部字段。

### 2.3 调度接入

`_housekeeping_loop` 每 6 小时调一次 `run_all()`，后者现在会串联：

1. `purge_published_outbox(older_than_days=7)`
2. `archive_hot_event_store(older_than_days=14, max_batches=6)`（**新**）
3. `purge_old_archive_events(older_than_days=90)`

顺序："先搬进来再删老的" 保证归档表不会跳过任何早期数据。

### 2.4 Backfill 脚本

路径：`scripts/maintenance/backfill_event_store_archive.py`

防呆双层保护：
- 默认模式 = dry-run（不传任何 flag 走预演）
- `--apply` 必须配合 `--confirm`（缺任一直接拒绝，返回码 4）

退出码语义：
- 0 成功 apply
- 2 dry-run 完成
- 3 DB 不可达 / profile 无 Postgres 配置
- 4 未显式 confirm 但传了 apply

---

## 3. 测试结果

### 3.1 单元测试

```
D:\文件\...\.venv\Scripts\python.exe -m pytest \
  tests/unit/test_event_store_archive_housekeeping.py -x -q
............                                                             [100%]
12 passed in 0.86s
```

同时跑了关键字过滤 `-k "housekeep or archive"`，全通过：

```
............                                                           [100%]
14 passed, 2428 deselected in 7.81s
```

覆盖场景：

| # | 测试名 | 验证 |
|---|-------|------|
| 1 | `test_archive_moves_old_rows_and_keeps_new_ones` | Happy path（3 老+2 新 → 只搬 3 老） |
| 2 | `test_batch_size_forces_multiple_rounds` | 25 行 + batch=7 → 4 批 |
| 3 | `test_idempotent_second_run_is_noop` | 二次执行无副作用 |
| 4 | `test_idempotent_with_preexisting_archive_rows` | archive 已有同 event_id 场景 |
| 5 | `test_cutoff_uses_strict_less_than` | cutoff 边界 `<` 而非 `<=` |
| 6 | `test_rollback_on_insert_failure_preserves_hot_table` | INSERT 失败事务 rollback，hot 完整 |
| 7 | `test_dry_run_counts_but_does_not_mutate` | dry_run 只统计不写 |
| 8 | `test_max_batches_bounds_execution` | max_batches=2 + 15 行 + batch=5 → 只搬 10 |
| 9 | `test_archive_report_as_dict_shape` | ArchiveReport 序列化 |
| 10 | `test_archive_report_as_dict_handles_none` | None 字段兼容 |
| 11 | `TestRunAllIntegration.test_run_all_includes_archive_hot_report` | run_all 串联 |
| 12 | `TestRunAllIntegration.test_run_all_can_disable_hot_archive` | `hot_event_archive_enabled=False` 跳过 |

### 3.2 Backfill CLI

```
$ python scripts/maintenance/backfill_event_store_archive.py --help
usage: backfill_event_store_archive.py [-h]
                                       --profile {spot,derivatives,spot_live,derivatives_live}
                                       [--older-than-days OLDER_THAN_DAYS]
                                       [--batch-size BATCH_SIZE] [--dry-run]
                                       [--apply] [--confirm]
                                       [--max-batches MAX_BATCHES]
...
rc: 0
```

在 Windows venv（无 psycopg2）验证了 arg parsing 路径。真实连 Postgres
的 dry-run 需在 WSL2 中执行，但首先要先把 worktree 代码 merge 到主分支
+ sync，按任务约束"不 push/不 deploy" 留给用户手动触发。

---

## 4. Backfill Dry-run 预估（基于 SQL 预演）

在 WSL2 `aats-postgres` 上直接跑 Pure SQL 等价查询（**不改数据**）：

### 4.1 当前库状态（2026-04-19 22:00 采样）

| 指标 | 值 |
|------|-----|
| `event_store` 行数 | 411,365 |
| `event_store` 时间跨度 | 2.87 天（2026-04-17 04:47 → 2026-04-20 01:23）|
| `event_store_archive` 行数 | 0 |

### 4.2 若跑 `--older-than-days=14`

| 指标 | 值 |
|------|-----|
| 待归档行数 | **0 行** |
| 理由 | 系统连续运行 2.87 天 < 14 天，无老数据 |
| 结论 | 安全，**等于 NOOP** |

**这是预期行为**：Phase 1 部署后到 2026-05-01 之前（即系统累积 14 天时），
housekeeping_loop 每次 tick 都是 NOOP，没有任何搬运。直到跨过 14 天门槛
才会开始正常工作。

### 4.3 若跑 `--older-than-days=1`（压测参考）

| 指标 | 值 |
|------|-----|
| 待归档行数 | 244,752 |
| Payload 总大小 | ~2.38 GB |
| 预计 batch 数（10k/批） | 25 批 |
| TOP 3 topic | `system.audit_records`(93k) / `strategy.sleeve_intents`(44k) / `system.guard_signal_updates`(33k / 1.17 GB) |

这是一个**可选**的 smoke test 模式——用 1 天 cutoff 让冷热分离管线
真跑一次，但**不推荐**在实盘做，原因：

- 2.38 GB 的 batch 搬运会暴涨 WAL
- archive 表会立刻装进 90 天 retention 的 90% 数据，Phase 1 优雅
  自然累积的节奏被打乱

---

## 5. 给用户的下一步清单

### 5.1 合并 / 部署前的准备

1. **Review 代码**（重点看 `housekeeping.py` 和 `_housekeeping_loop` 改动）
2. **确认交付物**：
   - [ ] `aats/storage/housekeeping.py`
   - [ ] `aats/bootstrap/config.py` (只改了 `_housekeeping_loop` 20 行)
   - [ ] `scripts/maintenance/backfill_event_store_archive.py`
   - [ ] `tests/unit/test_event_store_archive_housekeeping.py`
   - [ ] `docs/operations/event_store_archive_runbook.md`
   - [ ] `docs/design/event_store_retention_extension_design_2026_04_19.md` (Phase 1 已标完成)
   - [ ] `docs/review/path_b_event_store_archive_phase1_2026_04_19.md`

### 5.2 落盘流程（用户手动触发）

```bash
# Step A: 把 worktree commit 合回 main
# （由你手动确认 squash / 单 commit / 多 commit）

# Step B: 部署（标准流程）
cd D:\文件\project\AIParticipatingAutonomousTradingSystem
bash scripts/deploy.sh --skip-commit

# Step C: 部署后 1 小时内观察一次 housekeeping_loop
wsl -d Ubuntu bash -lc "docker logs --since=1h aats-gateway 2>&1 | grep db_housekeeping"

# 预期看到（系统未满 14 天时）：
# db_housekeeping_completed ... archive_hot_copied=0 archive_hot_deleted=0 archive_hot_batches=0
```

### 5.3 冷启动 backfill（可选）

**默认不需要** — 让 housekeeping_loop 自然生效即可。

若你希望在实盘主动做一次 smoke test（**有风险**，建议只在运维窗口做）：

```bash
# (1) 先 dry-run，确认规模
wsl -d Ubuntu bash -lc "cd ~/aats && source ~/aats-venv/bin/activate && \
  python scripts/maintenance/backfill_event_store_archive.py \
  --profile derivatives_live --older-than-days 14 --dry-run"

# (2) 如果上一步返回 "将归档 0 行" → 跳过 apply，Phase 1 交给自然调度
# (2') 否则，显式 apply
wsl -d Ubuntu bash -lc "cd ~/aats && source ~/aats-venv/bin/activate && \
  python scripts/maintenance/backfill_event_store_archive.py \
  --profile derivatives_live --older-than-days 14 --apply --confirm"
```

### 5.4 部署后监控 checklist

- [ ] `db_housekeeping_completed` 日志每 6h 出现一次（`docker logs aats-gateway`）
- [ ] `archive_hot_time_ms < 30000`（30 秒内完成）
- [ ] 系统运行到 2026-05-01 后，`archive_hot_copied` 字段开始非零
- [ ] `SELECT COUNT(*) FROM public.event_store_archive;` 跨过 14 天后开始增长
- [ ] `SELECT MIN(event_timestamp) FROM public.event_store;` 应稳定在 `now() - 14d` 附近

---

## 6. 风险 / 已知限制

### 6.1 Phase 1 范围内遗留

- **索引未变**：`event_store` 的 9 个索引在 2M+ 行规模时写入放大 会越来越
  明显。Phase 2/3 再优化。
- **Topic 分级保留未实现**：`system.guard_signal_updates` 每天 758 MB，
  占大头。Phase 2 再拆分 retention。
- **Prometheus 指标**：当前只有日志 event，无 metrics 导出。建议 Phase 2
  加 `aats_event_store_hot_rows` / `archive_lag_seconds` 等。

### 6.2 新增代码的方言依赖

- 实现中**刻意避免**使用 Postgres 专用 `ON CONFLICT DO NOTHING`，
  改为业务层 SELECT 去重 + 普通 INSERT。这样 SQLite 单测也能跑，
  但代价是多一次 SELECT round-trip。在 Postgres 层 10k 行的查询
  走 `ix_event_store_archive_event_id` UNIQUE index，成本 O(log n)
  × 10k，可忽略。

### 6.3 并发竞态

- 两个 Python 进程同时跑 `archive_hot_event_store`（e.g., housekeeping_loop
  + 手工 backfill 同时运行）可能出现：
  - A 读到 batch → B 也读到同样的 batch → A 先 INSERT + DELETE → B INSERT
    时被 UNIQUE index 拒绝（抛异常 → rollback）
  - A 的 hot 行已删 → B 的 SELECT 返回空 → B 正常退出
  - 两个 race 都不会产生数据错误，只是 B 白跑一轮
- **建议**：后台 housekeeping_loop 运行时，不要手动再跑 backfill
  脚本。runbook 已说明。

---

## 7. 疑问 / 待确认

1. **Phase 1 是否需要加 rate-limit flag 让运维能压低 max_batches？**
   当前 hardcoded 为 `max_batches=6`（60k 行/tick）。若要暴露成
   配置项，需改 `settings`（目前走默认参数）。Phase 2 前可以
   留着默认值观察。

2. **`hot_event_retention_days=14` 是否要做成 settings？**
   当前同样 hardcoded。Phase 1 约束不改配置层，我保持默认值。
   若需要动态调整（例如先配成 30 天观察），Phase 2 再加 env var。

3. **单测里事务 rollback 的 mock 是否够严格？**
   `test_rollback_on_insert_failure_preserves_hot_table` 用
   `__getattr__` 代理 session，通过 `compiled str` 识别 INSERT
   注入异常。已验证 hot 表最终未被改动，但用 Postgres 真实连接
   + testcontainers 跑一遍会更放心。建议 Phase 2 时加一个 integration
   test。

---

## 8. commit 计划（用户手动触发）

按任务"不 push"约束，我**未**自动 commit。建议的分片（语义化中文）：

```
feat(storage): Path B Phase 1 — archive_hot_event_store 热/冷分离
  aats/storage/housekeeping.py

feat(bootstrap): housekeeping_loop 日志加 archive_hot_* 字段
  aats/bootstrap/config.py

feat(scripts): event_store_archive 冷启动 backfill CLI
  scripts/maintenance/backfill_event_store_archive.py

test(storage): Path B Phase 1 单元测试（12 case）
  tests/unit/test_event_store_archive_housekeeping.py

docs: Path B Phase 1 运维 runbook + 设计文档 status 更新 + 完工报告
  docs/operations/event_store_archive_runbook.md
  docs/design/event_store_retention_extension_design_2026_04_19.md
  docs/review/path_b_event_store_archive_phase1_2026_04_19.md
```

或一个大 commit：

```
feat(storage): Path B Phase 1 — event_store 热/冷分离归档自动化
```

---

## 9. 结语

Phase 1 的核心价值是**从"半成品"变"自动化"**：`event_store_archive`
表原本已经有 schema 和 UNION 读路径，只缺一个定时调度入口。这次
落地：

- **代码量**：housekeeping.py +245 行 / config.py +15 行 / 脚本 +180 行
- **测试覆盖**：12 个 unit tests
- **侵入性**：几乎为 0（event_store 写入路径完全不动）
- **可回滚**：设个 `hot_event_archive_enabled=False` 就关，已归档
  数据通过 UNION 仍可读，无数据丢失

下一步（Phase 2）就是 topic 分级 retention，可等 Phase 1 跑满 14 天
稳态后再评估是否需要。
