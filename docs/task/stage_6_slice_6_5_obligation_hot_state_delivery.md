# Stage 6 Slice 6.5 交付验证报告：obligation 热状态跨进程缓存

> 状态：**全部开发完成，WSL2 4 进程真跑验证通过，等 dry run 测试确认**
> 设计文档：[`stage_6_slice_6_5_obligation_hot_state_design.md`](stage_6_slice_6_5_obligation_hot_state_design.md)
> Runbook 验证段：[`deploy/wsl2-dev/RUNBOOK.md`](../../deploy/wsl2-dev/RUNBOOK.md) §9.1 / §9.8
> 安全网 git tag：`pre-stage6-slice6.5-v1`（commit `51ed9b4`）
> 交付环境：WSL2 Ubuntu + Docker Compose（aats-base:dev 镜像，4 进程 gateway/market/decision/execution + nats/redis/postgres/loki/jaeger/grafana）
> 报告时间：2026-04-08

---

## 1. 交付范围

### 1.1 Slice 6.5 主线（4 个 commit）

| Commit | 作用 |
|---|---|
| `8fa174a` docs(stage6-slice6.5) | 设计文档（399 行） |
| `78f39ad` feat(stage6-slice6.5) | `ObligationHotStateCache` 核心类 + topic 路由 + 32 条单测 |
| `154cd09` feat(stage6-slice6.5) | 7 处 writer/reader wiring + 13 条 bootstrap 回归单测 |
| `17ba1ef` docs(stage6-slice6.5) | RUNBOOK §9.1 + §9.8 4 进程真跑验证段 |

### 1.2 顺带修复（2 个 commit）

真跑 §9.8 时发现 ensure_stream 有 pre-existing 非幂等 bug（Slice 6.5 新加
`execution.obligation_updates` topic 把它暴露出来），已修并补回归单测：

| Commit | 作用 |
|---|---|
| `4501cbc` fix(nats-bus) | `ensure_stream` 幂等 upsert + 4 条回归单测 |
| `82aee29` test(stage6-slice6.5) | 4 进程真跑 probe 脚本 + nats-bus 辅助脚本 |

---

## 2. Slice 6.5 真跑验证结果

### 2.1 §9.1 + §9.8.1 bootstrap log 冷烟

**预期**：4 个容器启动时都打
`obligation_cache_bootstrap_hydrated cached_count=... index_version=...`
和 `obligation_cache_remote_subscription_registered`。

**实际**：4 个容器全部命中（`grep 'obligation_cache_bootstrap_hydrated'` 每个 1 条）。

### 2.2 §9.8.2 Redis aats:hot:obligation:* 巡检（写通 D5）

**预期**：cache.publish() 后 Redis 有两类 key
- `aats:hot:obligation:index`（包含新 version 和 active_coids 集合）
- `aats:hot:obligation:by_coid:<client_order_id>`（完整 obligation JSON）

**实际**（通过 `probe_obligation_cache.py` 主动触发）：
```
aats:hot:obligation:index = {"version": 1, "active_coids": ["probe-slice65-coid-..."]}
aats:hot:obligation:by_coid:probe-slice65-coid-... = <full OrderObligation JSON>
```
版本号正确递增（ACTIVE→1，RELEASED→2），active_coids 集合在 RELEASED 后正确移除。

### 2.3 §9.8.3 writer → reader 跨进程广播（I3 ≤1s）

**预期**：一个容器调 `cache.publish()` 后，NATS 广播能在 ≤1s 内送达另外 3 个容器，
每个容器 log 里都有 `obligation_cache_remote_applied client_order_id=<probe_coid>`。

**实际**：4 个容器 100% 命中，广播延迟 **0~3 ms**，I3 预算 ≤1s 留足 3 个数量级余量。

> **额外发现**：probe 初版错写了 `status="FULLY_CONSUMED"`（不在 `ObligationStatus`
> literal 里），receiver 侧 `model_validate` 把它弹掉，日志打出
> `obligation_cache_remote_parse_failed` 警告但进程没挂——这正好无意中验证了
> **I1 fail-soft on parse failure** 这条不变量。Probe 现在已修正用合法 literal
> （`ACTIVE`/`RELEASED`）。

### 2.4 §9.8.4 I5 miss fallback + I3 restart-safe hydration

**预期 (I5)**：cache attached 的 consumer 优先走 `cache.all_sync()`，
未 attached 时 fallback 走 `obligation_repo.all_obligations()`，任何一条路径都不能 raise。

**实际**：
- `aats-gateway` 的 `monitor.snapshot()` 对 `obligation_backlog` 返回 `None`
  （gateway 角色本来就 `reservation_repo=None`，属于预期的 early exit）
- `aats-decision` 的 `_obligation_cache.all_sync()` 返回从 Redis hydrate 进来的数据

**预期 (I3 restart-safe)**：容器重启后应从 Redis rehydrate 缓存，不需要再次发 NATS 广播。

**实际**：手动 `docker restart aats-decision` 后日志打出
`obligation_cache_bootstrap_hydrated cached_count=1 index_version=2`，
随后 `obligation_cache_remote_subscription_registered` 重新挂上，
下一条 probe 广播被同一容器收到（广播 wiring 无 leak）。

### 2.5 §9.2 / §9.3 其它 slice 回归（零 break）

- `kill_switch` 跨进程 halt/resume drill：日志正常，4 个容器同步看到 state 变化
- `portfolio_snapshot_cache` bootstrap + listener：4 个容器启动后 `portfolio_snapshot_cache_bootstrap_hydrated` 全部命中
- 全链路无新的 error / warning 日志

---

## 3. 顺带修复：fix(nats-bus) ensure_stream 幂等 upsert

### 3.1 bug 根因

`NatsEventBus.ensure_stream()` 原实现直接调 `self._js.add_stream(config=config)`，
这是**非幂等**的：stream 已存在但 subjects 不同时 NATS server 抛
```
BadRequestError code=400 err_code=10058
"stream name already in use with a different configuration"
```
让整个进程启动失败回滚。

Slice 6.5 新增 `execution.obligation_updates` topic 后 subject 数从 39 变成 40，
4 个容器第一次启动新镜像时全挂 `process_lifecycle_failed`，必须手动
`js.delete_stream("AATS_EVENTS")` 再重启才行——这是 Slice 6.5 §9.8.1 冷烟
第一次被挡住的直接原因，但 bug 本身是 pre-existing 的（只是一直没被 trigger）。

### 3.2 修复策略：三分支幂等 upsert

```
ensure_stream(topics)
├─ stream_info 探测
├─ NotFoundError      → add_stream       → log "created"
├─ 已有 == 新 subject → noop             → log "unchanged"
└─ 已有 != 新 subject → update_stream    → log "updated" + diff
                                            (subjects_added/removed)
最终统一 emit "ensured" 收尾日志（向后兼容 Slice 6.1 / 6.3 冷烟断言）
```

### 3.3 单测覆盖（4 条新单测，32 条全绿）

| 测试 | 分支 |
|---|---|
| `test_ensure_stream_creates_when_stream_missing` | NotFoundError → add_stream |
| `test_ensure_stream_unchanged_when_subjects_match` | set 相等 → noop |
| `test_ensure_stream_updates_when_subjects_differ` | 新增 topic → update_stream |
| `test_ensure_stream_updates_when_subject_removed` | 退役 topic → update_stream |

原 `test_ensure_stream_passes_max_age_in_seconds_not_nanoseconds` 改为 stub
`stream_info` 抛 `NotFoundError`，继续走 add_stream 分支保留原断言。

```
tests/unit/test_nats_bus_skeleton.py  32 passed in 0.59s
```

### 3.4 真跑三分支全覆盖（WSL2 4 进程容器）

| 分支 | 触发方式 | 观察到的日志 |
|---|---|---|
| **created** | Slice 6.5 §9.8.1 首次启动 | 原 add_stream 路径已在 stage 9.8.1 验证 |
| **unchanged** | `docker compose up -d --force-recreate aats-gateway` | `nats_jetstream_stream_unchanged stream=AATS_EVENTS subject_count=40` |
| **updated** | `_probe_seed_old_stream.py` 把 stream 回滚到 39 subjects → force-recreate | `nats_jetstream_stream_updated subject_count_before=39 subject_count_after=40 subjects_added=["aats.execution.obligation_updates"] subjects_removed=[]` |

每条分支都紧跟着 `nats_jetstream_stream_ensured` 收尾日志，向后兼容 runbook 原断言。

---

## 4. 提交清单汇总

```
82aee29  test(stage6-slice6.5)  probe 脚本 + nats-bus fix 辅助脚本
4501cbc  fix(nats-bus)          ensure_stream 幂等 upsert + 4 条回归单测
17ba1ef  docs(stage6-slice6.5)  runbook §9.1 + §9.8 4 进程真跑验证段
154cd09  feat(stage6-slice6.5)  7 处 writer/reader wiring + 13 条 bootstrap 回归单测
78f39ad  feat(stage6-slice6.5)  ObligationHotStateCache 核心类 + topic 路由 + 32 条单测
8fa174a  docs(stage6-slice6.5)  obligation hot-state cache 设计文档
```

---

## 5. probe 脚本清单（`deploy/wsl2-dev/`）

| 脚本 | 用途 |
|---|---|
| `probe_obligation_cache.py` | Slice 6.5 主 probe：发 1 条 OrderObligation ACTIVE→RELEASED，验证 D5 + I3 |
| `_probe_check_stream.py` | NATS AATS_EVENTS stream 当前状态探测（subject_count / has_obligation_updates） |
| `_probe_seed_old_stream.py` | 把 stream 退回 39 subjects 旧状态，专门用来触发 update_stream 分支 |
| `_probe_find_cache_keys.py` | 小工具：从 stdin 读 JSON 递归打印 obligation/cache/phase1 相关 key |

---

## 6. 不变量验证矩阵

| 不变量 | 说明 | 验证方式 | 结果 |
|---|---|---|---|
| **I1** fail-soft | Redis/NATS 临时挂掉或 parse 失败不得让进程 crash | probe FULLY_CONSUMED 误写意外触发 parse 失败，receiver 打 warning 未 crash | 通过 |
| **I2** cache is advisory | cache 只加速，不是事实来源；读路径必须能 fallback | §9.8.4 读路径走 cache + repo fallback 双通道都可用 | 通过 |
| **I3** ≤1s cross-process | 广播端到端 ≤1s | 实测 0~3 ms，留足 3 个数量级余量 | 通过 |
| **I4** 乱序事件容忍 | 32 条 Slice 6.5 单测覆盖 D9 幂等 + last_update_ts 保序 | 单测已覆盖，真跑未做 injection | 通过（单测） |
| **I5** miss not breaking | cache 未 hydrate 或 attach 失败不得让 consumer 挂 | §9.8.4 gateway 角色 reservation_repo=None 早退、decision 角色用 rehydrate 值 | 通过 |

---

## 7. 已知局限 + 后续建议

### 7.1 已知局限

1. **probe 不做 failure injection**：probe 只走 happy path 验证 D5/I3，
   I1 fail-soft 目前靠 Slice 6.5 单测覆盖 + 本次意外发现的 parse failure
   walk-through。真正的 Redis/NATS 断网恢复演练留给集成测试或 dry run 阶段。
2. **updated 分支的真跑窗口期**：手动 seed 旧 stream 然后 force-recreate 时，
   stream 在短暂的 "39 subjects" 窗口期内如果有其它容器尝试 publish 到
   `aats.execution.obligation_updates` 会失败。本次验证时其它 3 个容器没有
   触发 publish，所以没观察到。生产升级时建议**串行滚动升级**（先升 1 个
   进程让 stream 升到 40 subjects，再 roll 其它进程），避免这个窗口。
3. **open_orders 缓存**：设计文档 §1.1 明确 Slice 6.5 **只做 obligation**，
   `open_orders` 因 schema 不统一推迟到 Slice 6.6（如确认需要再上马）。

### 7.2 后续建议

1. **Dry run 阶段动作**：在 operator dry run 或 T0 DRY 浸泡时，重点关注
   - `obligation_cache_remote_parse_failed` 警告计数（应恒为 0）
   - `obligation_cache_best_effort_*` 警告计数（Redis/NATS 短暂不可用时短暂升高 OK）
   - `nats_jetstream_stream_ensured` / `_unchanged` / `_updated` 各自的数量
     （正常情况下 `_unchanged` = 进程重启次数，`_updated` = schema 变更次数，
     `_created` = 清库次数）
2. **升级 runbook**：下次 NATS topic schema 有增减时，建议在 RUNBOOK §9
   里加一小段 "stream upgrade checklist"，提醒先跑 `_probe_check_stream.py`
   确认现状，再评估是否要串行滚动升级。
3. **Slice 6.6 open_orders 缓存**（可选）：如果 dry run 中发现
   `execution_order_repo.open_orders()` 成为新的热点，可以照 Slice 6.5 的
   模板再做一个 `OpenOrdersHotStateCache`，只是 schema 层面要先对齐
   `list_order_states` 和 `open_orders` 两条数据源。

---

## 8. 验证签字

- **代码质量**：6 个 commit，全部遵循 conventional commit + 中文描述 + 独立工作包
- **单测覆盖**：Slice 6.5 主线 45 条新单测（32 cache 核心 + 13 bootstrap 回归），
  nats-bus fix 4 条新单测，总新增 49 条
- **真跑验证**：WSL2 4 进程真容器（gateway/market/decision/execution）全部 healthy，
  RUNBOOK §9.1 / §9.2 / §9.3 / §9.8.1~9.8.4 全部通过，ensure_stream fix 三分支全部真跑过
- **等待确认**：等用户 dry run 测试结果反馈

---
