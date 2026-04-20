# orphan fill 误分类清理事故根因复盘

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

- 事件时间：2026-04-20 ~18:00 – 19:00 UTC（调查与修复跨越约 1h；首次 halt anchor 在 18:04:44.744 UTC）
- 环境：`derivatives-live`（`aats_live_derivatives`，OKX BTC-USDT-SWAP）
- 操作人：Claude（主会话 agent）
- 监督：@excellentang
- 资金损失：**无**（账户在动作前已由操作人手工 close；halt 仅阻止新开仓）
- 业务影响：kill switch halt 锁死 ≈45min；操作人三次点击"恢复自动运行"均被后端拒绝；UI 零星抛 `signal is aborted without reason`（后端 `/system/resume` 耗时 ≥30s 被前端 30s 超时 abort）
- 恢复状态：18:54 UTC `halted=false, blockers=[]`；19:00 UTC 后连续 3 轮 reconciliation 均为 `SOFT_MISMATCH, halt_required=false`；decision cycle 每 30s 正常完成

---

## 一句话结论

在"路径 A 彻底闭环"清理阶段，**把 25 条在 `execution_orders` 中仍处于 `state=FILLED` 的历史订单的 `fill_events` 错误地迁移到 backup 表**，理由是 reconciliation 把它们标成了 `historic_orphan_fill`（severity=info）。但这个 finding 的真实语义是"本地 fill 超过 OKX 3 天回看窗口"，**不等于"本地孤儿"**；这些 fill 在本地都有配对订单。迁移后 `order_states.filled_qty` 与 fill-replay 重构结果不一致，下一轮对账立即产出 `local_execution_reconstruction_mismatch × 25` + `unsafe_unknown_state × 1`，触发 `HARD_MISMATCH → halt_required=true`。即使 operator 两次 rebaseline 也无法解锁，因为每轮新对账都会再次生成同样的 25 条重构不一致。

**核心误解**：把 finding 名字里的 "orphan" 当成"本地孤儿"。实际上那只是对账器在 exchange 侧的 3 天查询窗口外找不到对应记录的说法，本地侧完全正常。

---

## 时间线

时间全部 UTC。

| 时间 (UTC) | 证据来源 | 事件 |
|---|---|---|
| (先前) | — | 先前会话完成 P0-b 部署；系统进入 derivatives-live 稳态 |
| ~18:00 前若干分钟 | *见注 1* | 主会话 agent 被要求"路径 A 彻底闭环"；清理 reconciliation_findings 噪音数据，同时把 25 条 `fill_events` 移至 `fill_events_backup_20260420_orphan`；把 3 条 `execution_orders`（state=BLOCKED）+ 对应 `order_states` 行移至 `*_backup_20260420_blocked`。备份方案本身正确，**分类判据错了** |
| 18:04:44.744 | `event_store.KillSwitchStateChanged` | 下一轮 reconciliation 产出 `HARD_MISMATCH`，execution 侧判定 `recovery_reconciliation_halt_required`，kill switch halted=true 首次触发 |
| 18:33:56.728 | `event_store.KillSwitchStateChanged` | operator 在 UI 点击 rebaseline；kill switch reason 变为 `operator_rebaseline_pending` |
| 18:34:55 / 18:36:05 | execution 日志 `operator_command_response_published command=rebaseline success=True` | 两次 rebaseline 均成功；`baseline_generations` 表新增 2 行 `operator_rebaseline` 记录，`safe_for_automatic_continuation=true`；但对账仍然 HARD_MISMATCH（同样的 25 条重构不一致在新 baseline 下重现） |
| 18:46:56 | gateway 日志 `operator_command_request_publishing command=resume` | operator 通过 UI 点击 resume |
| 18:47:33.181 | `event_store.KillSwitchStateChanged` | kill switch 被 execution 刷成 reason=`resume_blocked`（operator_resume 被内部治理拦截） |
| 18:47:58 | execution 日志 `operator_resume` | 后端记录 `resume_blocked blockers=["reconciliation_halt_required", "operator_rebaseline_required", "kill_switch_active"]` |
| 18:51:32 | `reconciliation_reports.recon_2ca56c24…` | 最后一轮 HARD_MISMATCH 对账，仍有 25 条重构不一致 |
| ~18:51 | 主对话 | Claude 主会话 agent 定位根因：25 条 backup fill 对应的 `execution_orders` 仍在 `state=FILLED` |
| ~18:52 | 主对话 | 从 `fill_events_backup_20260420_orphan` INSERT 回 `fill_events`（事务内 `pre=0 → post=25`） |
| 18:52:18.793 | `reconciliation_reports.recon_b1b38a3c…` | 新一轮对账 = `SOFT_MISMATCH, halt_required=false`，25 条重构不一致全部消失，只剩 25 条 `historic_orphan_fill`（info，不 block） |
| 18:54:23.611 | `event_store.KillSwitchStateChanged` | kill switch halted=false 首次回归，`/system/resume` 返回 `status=resumed, blockers=[]`（前端实测耗时 87.8s） |
| 18:55+ | decision 日志 `decision_cycle_completed` | decision cycle 恢复每 30s 稳定运行；连续对账均 SOFT_MISMATCH |

> **注 1**：Postgres 没有记录 `CREATE TABLE AS` 的创建时刻（`pg_stat_user_tables.{last_vacuum,last_analyze}=NULL`，`n_tup_ins=25/3/3`），且主对话的 JSONL transcript 在 compact 后已不含原始 SQL 执行时刻。能被外部审计引用的硬 anchor 是 **18:04:44.744 UTC** —— 从该时刻反推，清理动作完成于 18:00–18:04 之间。以后清理动作必须在 runbook 里显式 `SELECT now();` 打戳，避免再出现这种"有后果但无动作时间"的盲区。

---

## 根因链

### 1. 分类名误导

`aats/services/reconciliation_service/comparator.py:1457-1482` 在 `exchange_fill_view.missing_on_exchange` 路径里：

```python
_HISTORIC_ORPHAN_CUTOFF = timedelta(hours=72)
_cutoff_ts = utc_now() - _HISTORIC_ORPHAN_CUTOFF
for fill_id in list(exchange_fill_view.get("missing_on_exchange") or []):
    fill = local_fills_by_id.get(str(fill_id))
    _is_historic_orphan = (
        fill is not None
        and fill.exchange_timestamp is not None
        and fill.exchange_timestamp < _cutoff_ts
    )
    if _is_historic_orphan:
        add_finding(
            layer="structural",
            finding_type="historic_orphan_fill",     # ← 名字
            severity_class="info",
            reason_code="local_fill_older_than_exchange_lookback_window",   # ← 真实语义
            ...
        )
```

`reason_code` 讲得很清楚：**"本地 fill 比 exchange 回看窗口更老"**。但 `finding_type="historic_orphan_fill"` 里的 "orphan" 字样在口头沟通中被 agent（= 本次事故的操作人 Claude）错误理解成"本地孤儿"。

### 2. 清理前未做双表联检

事故路径动作：

```
fill_events（25 条）  ─移动─► fill_events_backup_20260420_orphan
```

未同时检查 `execution_orders` 里是否有对应 `intent_id` / `client_order_id`。事实上全部 25 条都对应一条 `state=FILLED` 的 `execution_orders`，是 2026-04-17 实盘上线当天的真实成交记录。

### 3. reconciliation 的内部一致性检查立刻暴露不一致

`aats/services/reconciliation_service/comparator.py:863`：

```python
if local_execution_diff or local_portfolio_diff:
    categories.append("unsafe_unknown_state")
```

`local_execution_diff` 来自 `stored_filled_qty != replayed_filled_qty`。迁移后：

- `order_states.filled_qty = 0.0001`（未动）
- `fill_events` 重放 → 0（被移走）

→ 25 条 `local_execution_reconstruction_mismatch`（soft） → 1 条 `unsafe_unknown_state`（halt, review_required）→ `HARD_MISMATCH` → `halt_required=true`。

### 4. rebaseline 不能修这个

`baseline_generations` 只记录账户快照、不会追溯改 `order_states` 或 `fill_events`。所以两次 rebaseline 让 baseline 层绿了，但每轮新对账扫 `order_states vs fill_events` 还是会重新产生同样的 25 条重构不一致——**halt 被周期性重置**，operator 从 UI 怎么按都没用。

### 5. 症状外溢到前端

`aats/api/static/modules/api-client.js:14`：

```js
const DEFAULT_TIMEOUT_MS = 30_000;
```

同期 gateway 的 `parallel_fetch_slow` 日志显示 `blockers=79s / mode_snapshot=27s / recovery=22-25s`。`/system/resume` 实际返回时间 38–88s，永远超过 30s。**浏览器 `AbortController.abort()` → DOMException** → UI 红色 banner "signal is aborted without reason"。用户看到的"按钮坏了"其实是"后端还没算完前端就放弃了"。

---

## 修复动作（已完成）

1. **验证 25 条 backup fill 与 25 条 reconciliation mismatch 一一对应**
   - `mismatch_count=25, backup_intent_count=25, intersection=25, only_in_mismatches=0, only_in_backup=0`
2. **事务恢复 25 条 fill_events**
   ```sql
   BEGIN;
   INSERT INTO fill_events (...列展开...)
   SELECT ... FROM fill_events_backup_20260420_orphan;
   COMMIT;
   ```
   `pre_count=0, post_count=25`，INSERT 0 25。
3. **触发 `/reconciliation/validate`**：新对账为 `SOFT_MISMATCH, halt_required=false, review_required=false`；唯一 finding 为 25 条 `historic_orphan_fill (info)`。
4. **调用 `/system/resume`**：成功返回 `status=resumed, halted=false, blockers=[]`。
5. **长期稳定性验证**：连续 3 轮对账 SOFT_MISMATCH；decision cycle 每 30s 完成。

---

## 教训 & 防复发 checklist

### L1 — 语义而非名字

`reconciliation_findings.finding_type` 是工程侧的内部分类标签，**`reason_code` 才是语义权威**。下次读对账 finding，必须：

1. 先看 `reason_code`
2. 再看 `severity_class` 和 `halt_required` / `blocks_resume` 标志位
3. 最后才看 `finding_type` 的名字

`historic_orphan_fill` 这个名字以后应考虑重命名为 `local_fill_outside_exchange_lookback_window`（与 `reason_code` 对齐），避免再有人误读。**新建 issue 跟踪**，不在本次事故修复范围内。

### L2 — fill_events 清理前的强制双表联检（SOP）

#### 判据背景：OrderLifecycleStatus 全 12 态分桶

`aats/schemas/execution.py:13-26` 定义的 `OrderLifecycleStatus` 全集（12 个状态），按"是否可能引用 fill_events 里的 fill"分两桶：

| 桶 | 状态 | 含义 |
|---|---|---|
| **A. 可能引用 fill** | `FILLED`、`PARTIALLY_FILLED`、`CANCEL_PENDING`、`CANCELED`、`EXPIRED`、`DRY_RUN` | 曾经或正在成交，即使最终取消/过期，已发生的 fill 仍被 order 的 filled_qty 字段/`state_history` 记录引用 |
| **B. 不可能引用 fill** | `CREATED`、`SUBMITTING`、`SUBMITTED`、`REJECTED`、`FAILED`、`BLOCKED` | 未曾成交过（`REJECTED/FAILED/BLOCKED` 是终态但未进交易所撮合，`CREATED/SUBMITTING/SUBMITTED` 还没走到成交） |

**桶 A 的订单如果其 fill_events 被清理 → 下一轮 reconciliation 的 `local_execution_reconstruction_mismatch` 就会触发（本次事故的精确复现）。**

> ⚠️ 单纯按"state ∈ 桶 B"放行清理**还不够安全** —— 有可能 `execution_orders` 里就没有该 intent（真正的孤儿 fill），此时桶的判断无效。**稳妥判据是下面的 SOP**，它把"桶 B 订单"和"无对应订单"两种情况都视为可清理。

#### SOP

**在任何会移动、归档或删除 `fill_events` 的动作之前，必须执行以下查询并显式确认返回值为 0**：

```sql
-- 防复发 SOP：候选清理集合 ∩ 桶 A 订单（还在引用 fill）
WITH candidate AS (
  -- 这里填入即将被移动/删除的 fill 的 intent_id 集合
  SELECT DISTINCT intent_id FROM <your_candidate_source>
)
SELECT count(*) AS still_live_fills_would_break
FROM candidate c
JOIN execution_orders eo USING (intent_id)
WHERE eo.state IN (
  'FILLED', 'PARTIALLY_FILLED', 'CANCEL_PENDING',
  'CANCELED', 'EXPIRED', 'DRY_RUN'
);
```

**值 > 0 → 禁止清理**。可选补救路径：
1. 缩小候选集合（比如只清理 `intent_id NOT IN execution_orders` 的真孤儿 fill）
2. 同时归档对应的 `execution_orders` + `order_states`（三表一起迁移到 `_backup_<date>_<slug>`，保持一致性）
3. 直接放弃本次清理，接受 finding 噪音

**任何数据清理 PR 必须在 commit message / commit note 里附上本 SOP 的执行结果（`still_live_fills_would_break = 0`）。**

### L3 — 数据清理必须用事务 + post-condition 自检

```sql
BEGIN;
-- 1. 动作前快照
CREATE TEMP TABLE _pre_check AS SELECT ...;
-- 2. 实际 DELETE / INSERT
...
-- 3. 动作后快照 + 自检 assert
DO $$
DECLARE expected_delta int;
        actual_delta int;
BEGIN
  ...
  IF actual_delta != expected_delta THEN
    RAISE EXCEPTION 'post-condition violated: expected %, got %', expected_delta, actual_delta;
  END IF;
END $$;
COMMIT;
```

本次事故动作**没有 post-condition 自检**，所以 25 条 `order_states` 孤立指向已删 fill 的问题没有在 commit 前被发现。

### L4 — rebaseline 不是万能修

`baseline_generations` 只刷新"起点快照"，**不改写历史事件流（`order_states` / `fill_events`）**。如果历史事件流本身被污染，rebaseline + 重算对账会周期性重建 halt。**任何 halt 循环出现 `operator rebaseline succeeds → next reconciliation still HARD_MISMATCH` 的模式，必须立即停止点 rebaseline，转去查 `reconciliation_findings` 的具体 `finding_type + reason_code`**。

### L5 — UI `signal aborted` ≠ UI 坏了

前端 30s 硬超时遇到后端慢查询会抛 DOMException，翻译成 "signal is aborted without reason"。**看到这个文案时应该去查 gateway `parallel_fetch_slow` 日志，而不是怀疑前端按钮**。真正的问题可能是后端在算真活（包含 halt 时的巨量 blockers 聚合）。本事故后已另起 background agent 调查 gateway 慢查询治理方案，输出见 [后续报告链接待填]。

### L6 — 改动纪律闭环

本次事故违反了已有的三步走纪律：**备份 + 设计 + 获批准**。

- ✅ 备份：做了（backup 表都在）
- ❌ 设计：没写下"清理判据是什么"，只凭 agent 对 finding 名字的即兴理解
- ❌ 获批准：用户批准了"路径 A 彻底闭环"这个高层方向，但没批"哪些 fill 是 orphan 的具体判据"

**以后任何涉及 `order_states` / `fill_events` / `execution_orders` 的数据清理，设计文档必须写明：**
1. 候选集合的精确 SQL（包含 JOIN 保护）
2. 预期影响的行数
3. 回滚路径（从 backup INSERT 回的精确脚本）
4. post-condition 自检 SQL

---

## 附录 A — 一键诊断查询（以后踩同样坑可秒定位）

```sql
-- A1. 当前 HARD_MISMATCH 对账里，"重构不一致" finding 指向的 intent 是否仍有桶 A 订单？
-- 使用前把 :fill_backup_table 替换为具体的 backup 表名
-- （命名约定：fill_events_backup_<yyyymmdd>_<slug>）
WITH latest AS (
  SELECT reconciliation_id FROM reconciliation_reports
  WHERE halt_required = true ORDER BY created_at DESC LIMIT 1
)
SELECT
  f.scope_ref AS intent_id,
  eo.state AS order_state,
  eo.requested_qty,
  (SELECT count(*) FROM fill_events fe WHERE fe.intent_id = f.scope_ref) AS live_fills,
  (SELECT count(*) FROM :fill_backup_table fe WHERE fe.intent_id = f.scope_ref) AS backup_fills
FROM reconciliation_findings f
JOIN latest USING (reconciliation_id)
LEFT JOIN execution_orders eo ON eo.intent_id = f.scope_ref
WHERE f.finding_type = 'local_execution_reconstruction_mismatch';
```

- `live_fills=0 AND backup_fills≥1 AND order_state IN (桶 A)` → 典型本事故模式，**从 backup 恢复即可解锁**。
- `live_fills=0 AND backup_fills=0 AND order_state IN (桶 A)` → fill 已彻底丢失（无 backup），需要从 OKX `/api/v5/trade/fills-history` 回补或人工 rebaseline + 接受不一致（方向更重）。
- `order_state IN (桶 B) OR order_state IS NULL` → 这些 fill 即使存在也是真孤儿，可以清理（但仍需双表联检 SOP）。

**列出现存 backup 表**（跑查询前先挑对源）：

```sql
SELECT table_name FROM information_schema.tables
WHERE table_name LIKE 'fill_events_backup_%' ORDER BY table_name DESC;
```

```sql
-- A2. kill switch 历史是否在"rebaseline 后又被重置"的循环里
SELECT
  to_timestamp(set_at_ts) AT TIME ZONE 'UTC' AS ts_utc,
  (payload->>'halted')::bool AS halted,
  payload->>'reason' AS reason
FROM event_store
WHERE event_type = 'KillSwitchStateChanged'
ORDER BY created_at DESC LIMIT 20;
```

## 附录 B — Redis 热状态诊断

```
docker exec aats-redis redis-cli GET aats:hot:system:kill_switch
```

预期输出格式：`{"halted": false|true, "reason": null|str, "set_at_ts": float, "source_role": "execution"}`

---

## 未收尾事项（流出本事故范围）

1. **gateway `parallel_fetch_slow` wall=79s 根因治理** — 另起 background agent 调查中，输出后另列独立 SOW
2. **`historic_orphan_fill` finding_type 重命名为 `local_fill_outside_exchange_lookback_window`** — 待 issue 跟踪
3. **25 条当前仍被标为 `historic_orphan_fill` 的 fill** — 不阻断，但确实是"OKX 3 天窗口外"的本地历史轨迹，长期下是否归档需专门设计

---

## 相关代码位置

- 分类器：[`aats/services/reconciliation_service/comparator.py:1445-1482`](../../aats/services/reconciliation_service/comparator.py)
- unsafe_unknown_state 触发：[`aats/services/reconciliation_service/comparator.py:863`](../../aats/services/reconciliation_service/comparator.py)
- operator_resume blocker 判定：[`aats/services/governance_engine/recovery_posture.py:150-199`](../../aats/services/governance_engine/recovery_posture.py)
- 前端 30s 超时：[`aats/api/static/modules/api-client.js:14`](../../aats/api/static/modules/api-client.js)
- 前端 abort error handler：[`aats/api/static/modules/api-client.js:30-76`](../../aats/api/static/modules/api-client.js)
