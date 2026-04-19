# event_store 归档运维手册 (Path B Phase 1)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 适用范围：`aats_live_derivatives.public.event_store` / `public.event_store_archive`
>
> 本文档 Phase 1 落地于 2026-04-19，后续 Phase 2/3 参见 `docs/design/event_store_retention_extension_design_2026_04_19.md`。

---

## 1. 背景与目标

`event_store` 是主事件总线的落盘表，所有 envelope 进入系统后永久留存。
Phase 1 启用热/冷分离：

- **热表 (`event_store`)**：保留最近 **14 天** 数据，高频读写路径。
- **冷表 (`event_store_archive`)**：存放 > 14 天的数据，读取走 `list_envelopes`
  UNION，应用层无感。

目标：把"系统运行几天，热表就累积几天"的行为，变成"热表稳定在 14 天内"，
给 calibration 查询提供充足样本的同时控制写入放大 / TOAST 膨胀。

---

## 2. 正常运行状态

### 2.1 自动调度

后台任务 `_housekeeping_loop` 每 **6 小时**触发一次 `run_all`：

- 1) `purge_published_outbox(older_than_days=7)`
- 2) `archive_hot_event_store(older_than_days=14, batch_size=10_000, max_batches=6)`
- 3) `purge_old_archive_events(older_than_days=90)`

单个 tick 的归档上限：6 个 batch × 10k 行 = **60k 行/tick**。按日速率
144k 行/天 计算，4 个 tick 就能追平。冷启动 400k+ 行的 backfill 推荐走
手工脚本（见 §4），避免 housekeeping_loop 单 tick 过长。

### 2.2 健康指标

日志事件 `db_housekeeping_completed`，关键字段：

| 字段 | 含义 | 健康阈值 |
|------|------|---------|
| `archive_hot_copied` | 本 tick INSERT 进 archive 的行数 | 稳态 < 60_000（即 max_batches × batch_size） |
| `archive_hot_deleted` | 本 tick DELETE 掉 hot 的行数 | 稳态约等于 copied |
| `archive_hot_batches` | 本 tick 跑了几个 batch | 通常 0–3，持续 = max_batches 表示有积压 |
| `archive_hot_time_ms` | 总耗时 | < 30_000 (30s)；超过要关注锁竞争 |

一次性 SQL 观测：

```sql
-- 热表行数 & 时间窗口
SELECT COUNT(*) AS hot_rows,
       MIN(event_timestamp) AS oldest_ts,
       MAX(event_timestamp) AS newest_ts,
       NOW() - MIN(event_timestamp) AS retention_actual
FROM public.event_store;

-- 归档表行数 & 时间窗口
SELECT COUNT(*) AS archive_rows,
       MIN(event_timestamp) AS oldest_ts,
       MAX(event_timestamp) AS newest_ts
FROM public.event_store_archive;

-- 热表中 > 14 天的积压（稳态应持续接近 0）
SELECT COUNT(*) AS stale_rows,
       MIN(event_timestamp) AS oldest_stale_ts
FROM public.event_store
WHERE event_timestamp < NOW() - INTERVAL '14 days';
```

---

## 3. 手动执行

### 3.1 手动归档入口

项目有 **两个** 手动入口：

| 入口 | 用途 | 路径 |
|------|------|------|
| `scripts/archive_event_store.py` | 老 CLI（运维层），以绝对 ts/时长驱动 | 用于临时小规模归档 |
| `scripts/maintenance/backfill_event_store_archive.py` | **新增** Phase 1 backfill 入口 | 冷启动 / 幂等 dry-run |

### 3.2 冷启动 backfill（首次上线用）

```bash
# 1. 先 dry-run 看规模
wsl -d Ubuntu bash -lc "cd ~/aats && source ~/aats-venv/bin/activate && \
  python scripts/maintenance/backfill_event_store_archive.py \
  --profile derivatives_live --older-than-days 14 --dry-run"

# 输出示例：
# [backfill] cutoff_ts=2026-04-05T...
# [backfill] 将归档 X 行，最早 ts=Y
# 退出码 2 = dry-run 完成

# 2. 如果规模可接受 → 实际跑
wsl -d Ubuntu bash -lc "cd ~/aats && source ~/aats-venv/bin/activate && \
  python scripts/maintenance/backfill_event_store_archive.py \
  --profile derivatives_live --older-than-days 14 --apply --confirm"
```

**关键点**：

- `--apply` **必须**与 `--confirm` 同时传，缺少 `--confirm` 直接拒绝（返回码 4）。
- 脚本幂等：可以反复跑，archive 里已存在的 event_id 会被过滤，不会重复。
- 每 10_000 行一个事务 → 单事务 < 100 MB WAL，不会把 Postgres 锁住。
- 无 `--max-batches` 默认搬完为止。首次冷启动 400k+ 行 ≈ 40 个 batch ≈ 5–10 分钟。

### 3.3 老 CLI 快速查询

```bash
# 只看归档表当前状态，不改数据
wsl -d Ubuntu bash -lc "cd ~/aats && source ~/aats-venv/bin/activate && \
  python scripts/archive_event_store.py \
  --profile derivatives_live --summary-only"
```

---

## 4. 故障排查

### 4.1 热表行数持续增长，archive 表没动

**可能原因**：

1. `housekeeping_loop` 没在运行 → 检查 `aats-gateway` / `aats-execution` 容器
   日志里有没有 `db_housekeeping_completed`（每 6h 一次）。
2. slice 门控 `_slice_active("execution", ...)` 阻止了调度 → 当前进程
   role 不是 `execution` / `monolith`。
3. DB 不可用 → 看 `background_loop_failed` 带 `subsystem=db_housekeeping`
   的错误。

**排查命令**：

```bash
# 最近 1 小时 housekeeping 日志
docker logs --since=1h aats-gateway 2>&1 | grep db_housekeeping

# 检查 archive 表当前行数
docker exec -i aats-postgres psql -U admin -d aats_live_derivatives \
  -c "SELECT COUNT(*) FROM public.event_store_archive;"
```

### 4.2 archive_hot_event_store 超时或报错

**症状**：日志 `background_loop_failed subsystem=db_housekeeping`

**典型原因**：

- `ix_event_store_event_timestamp` 索引缺失 → 批量查询 cutoff 走全表扫
- WAL 占用 / VACUUM 未完成 → `vacuum verbose public.event_store;`
- 事务超时（单 batch > 5 min）→ 调小 `hot_event_batch_size` 或 `max_batches`

**手工降级**：临时关掉热表归档，保留其他清理：

```python
# 在 python 里直接调
housekeeping.run_all(
    hot_event_archive_enabled=False,  # ← 关闭
)
```

或编辑 `aats/bootstrap/config.py::_housekeeping_loop` 把该调用改为
`run_all(hot_event_archive_enabled=False)` 然后重启容器。

### 4.3 读路径读不到老数据

**应该不会发生**。`event_store_postgres.py` 中 `between()`、`by_topic()`、
`by_decision()` 等都已经 UNION archive 表。若发现某个调用方直接
`SELECT * FROM event_store`，**那是 bug**，要改为走 `PostgresEventStore`。

**定位方法**：

```bash
# 搜所有直接 SQL 操作 event_store 的 Python 代码
grep -rn "FROM event_store\b" aats/
```

### 4.4 归档表行数不对齐

**症状**：某些 event_id 既在 hot 又在 archive（重复）。

- **通常情况**：归档过程中 INSERT 成功、DELETE 失败 → 下次 backfill 会
  把幂等分支走一遍，archive 端跳过同 event_id，重新 DELETE hot。最终
  一致。
- **异常情况**：archive.event_id 有两条不同 source_sequence_id → 说明
  `event_id` UNIQUE 约束被绕过，属于严重 bug，立刻报警。

---

## 5. 监控与告警 (TODO Phase 2)

目前 Phase 1 仅依赖 `db_housekeeping_completed` 日志。Phase 2 建议加
Prometheus 指标：

- `aats_event_store_hot_rows`
- `aats_event_store_archive_rows`
- `aats_event_store_archive_lag_seconds`（= now - MIN(hot.event_timestamp) - retention）
- `aats_event_store_archive_tick_duration_ms`

告警规则建议：

- `hot_rows > 3_000_000` 连续 1h → WARNING
- `archive_lag_seconds > 2_592_000`（30d）→ CRITICAL（说明 archive 完全停摆）
- `archive_tick_duration_ms > 60_000` 连续 3 次 → WARNING

---

## 6. 回滚

若 Phase 1 引入线上问题：

1. **关闭热表归档**（保留 outbox/archive purge）：
   编辑 `aats/bootstrap/config.py::_housekeeping_loop` 调 `run_all`
   时传 `hot_event_archive_enabled=False`（或临时注释掉对 `housekeeping`
   的调用整个 loop）。
2. **redeploy**：`bash scripts/deploy.sh --skip-commit`。
3. **数据影响**：已归档的行仍在 `event_store_archive`，读路径 UNION
   能看到，完全无数据丢失。hot 表会重新无限增长，但不会阻断业务。

若需要把数据从 archive 倒回 hot（极端场景）：

```sql
-- 极端场景：Phase 1 实现有 bug 需要把 archive 倒回 hot
INSERT INTO public.event_store (
    event_id, schema_version, created_at, event_type, event_timestamp,
    source_component, topic, event_key, decision_id, symbol, timeframe,
    product_type, margin_mode, payload
)
SELECT event_id, schema_version, created_at, event_type, event_timestamp,
       source_component, topic, event_key, decision_id, symbol, timeframe,
       product_type, margin_mode, payload
FROM public.event_store_archive a
WHERE NOT EXISTS (
    SELECT 1 FROM public.event_store h WHERE h.event_id = a.event_id
);
-- 然后清空 archive
TRUNCATE TABLE public.event_store_archive;
```

**注意**：`sequence_id` 会重新编号（自增），与原值不一致；任何依赖
`sequence_id` 做偏移的代码会乱序。只有在紧急且明确接受代价时才跑。

---

## 7. 附录：代码位置索引

| 项目 | 路径 |
|------|------|
| 主函数 `archive_hot_event_store` | `aats/storage/housekeeping.py::DatabaseHousekeeping.archive_hot_event_store` |
| 返回报告 dataclass | `aats/storage/housekeeping.py::ArchiveReport` |
| 后台调度 loop | `aats/bootstrap/config.py::_housekeeping_loop` |
| backfill CLI | `scripts/maintenance/backfill_event_store_archive.py` |
| 老手工 CLI | `scripts/archive_event_store.py` |
| 单元测试 | `tests/unit/test_event_store_archive_housekeeping.py` |
| Phase 设计文档 | `docs/design/event_store_retention_extension_design_2026_04_19.md` |

---

## 8. 变更履历

- 2026-04-19 — Phase 1 初版（aa052ed34 worktree，agent Path B 任务）
