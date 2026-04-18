# Phase 1-4 v2 设计 · 第二次审查

> 审查对象:[rdp_scope_expansion_detailed_design_v2.md](./rdp_scope_expansion_detailed_design_v2.md)
> 审查视角:v2 新方案的隐藏缺陷、边界 case、并发安全、回滚完备性

## 审查总纲

v2 解决了 v1 的 10 blocker + 9 warning,但引入了新方案(尤其 saga、CAS、system_config)需要细审。

---

## R2-01 ❌ Saga Step 3 的 payload merge 语义不安全

**v2 设计**(§1.6):"把 research 里 threshold_patches 合进对应 profile 的 payload"

**问题**:
1. `strategy_profile_activation.payload` 包含 profile 的**全部**配置,远不止 threshold_patches。粗暴 merge 可能覆盖非 RDP 管辖字段(比如 `allowed_symbols`, `score_weights`)
2. 如果 live 侧 operator 在 Shadow 期手动改了 payload(人类干预),saga apply 会静默覆盖
3. payload 是 JSON 列,Postgres 没有 `jsonb_merge_deep`,要么拉整条到 Python merge 再写回(竞态),要么用 `jsonb_set` 逐 key(只改预期字段)

**建议(强制)**:
- 用 `jsonb_set` 只改白名单 key:`strategy_entry_min_signal_edge_bps` / `strategy_entry_alpha_min` / `strategy_min_net_edge_bps`
- 在 saga Step 3 前先 **SELECT FOR UPDATE** 目标行,校验:
  - 若 payload 里的三个 key 当前值 ≠ `threshold_patches.from`(research 读到的 baseline)→ abort saga,告警"live drift detected, manual reconcile"
  - 否则 jsonb_set 三次,写回

## R2-02 ❌ CAS streak 方向变化的误重置

**v2 设计**(§1.2):
```sql
WHEN clamp_violation_direction != EXCLUDED.direction THEN 1  -- 方向变了,重置
```

**问题**:
假设 streak 已经 = 2(连续两次 above_upper),第 3 周 research 产出 below_lower 的 clamp 超界——按 v2 逻辑重置为 1。但这恰恰是 operator **应该** 立即被警示的情况(profile 在反复震荡,参数系统已无法收敛)——重置反而让 review rec 永远触发不了。

**建议**:
- 方向变化也 ++ streak,但把 direction 改成 `mixed`
- streak=3 的 review rec 区分两个亚类:`direction='above_upper'` / `'below_lower'` / `'mixed'`
- UI/operator 看到 `mixed` 时更知道 profile 需要根本性重新 seed

## R2-03 ❌ batch_b_99_rollback 漏表

**v2 设计**(§1.1 rollback):只 drop `uq_active_profile/sleeve` index,未 drop Phase 1 新表

**问题**:
- batch_b_02 的 `governance.profile_research_runs` + `governance.profile_type_review_streak` 没 drop
- batch_b_03 的 `governance.cost_calibration_runs` 没 drop
- batch_b_04 的视图没 drop
- batch_b_01 的 `governance.system_config` 没 drop

**建议**:拆分 rollback 为 `batch_b_99_rollback.sql`(主)+ `batch_b_04_rollback.sql` / `batch_b_03_rollback.sql` / ... 分步 rollback。部署 SOP 强制 "按反序执行"。

## R2-04 ⚠ apply_token action 分流逻辑绕过风险

**v2 设计**(§1.5):"`apply_token.action=apply`,服务端按 `rec.scope` 决定双签"

**问题**:
- 若攻击者拿到 `action=apply` 的 token,可尝试伪造 combo rec_id 去应用 profile 级变更
- token 本身只带 action,不带 scope,token 合法不代表操作合法
- 双签的 actor_id 比较仅靠"两个不同 actor id"——同一 operator 可以用两个账号

**建议**:
- apply_token 生成时把 `recommendation_id` 和 `scope` 一并编入 HMAC input → 一个 token 只能用于一条 rec
- `/apply` 解析 token 后必须验证 `token.recommendation_id == URL.{id}`,不匹配 reject
- Profile / sleeve scope 的双签要求 actor 身份来自**不同 operator 角色组**(新增 `operators.role_group`),不是"两个不同 user_id"就行

## R2-05 ⚠ get_live_session 连接生命周期未定义

**v2 设计**(§1.7):"连接池大小: 5, 超时 10s"

**问题**:
- 连接池在什么时机创建?进程启动 eager 还是首次调用 lazy?
- 多线程 / async 跑时,pool 是共享的?还是 per-task?
- Postgres 的 idle connection 会被对端清,需要 `pool_recycle` + `pool_pre_ping`
- 跨库 saga 跑到 Step 3 如果 live pool 瞬时没连接,会等还是失败?没定义

**建议**:
```python
# aats/data_platform/runtime/session.py
_live_engine_rw = create_engine(
    os.environ["AATS_LIVE_DB_URL_RDP"],
    pool_size=3, max_overflow=2, pool_recycle=300, pool_pre_ping=True,
    pool_timeout=30,  # saga 失败 fast fail,不等超 30s
)
_live_engine_ro = create_engine(
    os.environ["AATS_LIVE_DB_URL_RDP"] + "?options=-c default_transaction_read_only=on",
    pool_size=2, max_overflow=2, pool_recycle=300, pool_pre_ping=True,
)
```
- eager 在 daemon 启动时初始化并做一次 ping,失败则 daemon refuse to start(fail-fast)

## R2-06 ❌ system_config 的并发写不保护

**v2 设计**(§1.8):"POST /rdp/system-config/{key}" flip flag

**问题**:
- 两个 operator 并发 flip 同一个 flag,没有乐观锁
- flag flip 没审计:flip 前的 value 是什么?谁改的?何时?

**建议**:
- `system_config` 表加 `version INTEGER NOT NULL DEFAULT 1`,UPDATE 走 CAS `SET value=:new, version=version+1 WHERE key=:k AND version=:v`;冲突 409
- 加姊妹表 `system_config_history(key, old_value, new_value, changed_by, changed_at)`, 每次 POST 落一条
- GET API 返回当前 version,客户端 flip 必须 submit 当前 version

## R2-07 ⚠ Profile grid "27 points × N profile × 90 天 replay" 实测未验

**v2 设计**(§1.2):降到 27 points,但没说 90 天 replay 的单次 Sharpe 计算要多久

**问题**:
- `gold.market_swap_replay_bars_5m` 每天约 288 条,90 天约 2.6w 条,每 grid point 扫一遍全量计算 Sharpe,27 × 2.6w ≈ 70w 行数据处理
- 5 个 active profile → 350w 行/run
- 如果没 cache replay bars,weekly 跑还能接受;若改 daily 会出问题
- workflow timeout 1800s 需要实测

**建议**:
- shadow 期打 metric `profile_research_duration_seconds`
- 若实测 > 1500s,切 coordinate descent(从 27 降到 9(3+3+3 分轮))
- replay bars 在 Python 进程内 LRU cache 一次,所有 grid point 复用

## R2-08 ❌ profile_apply_saga 幂等键生成有漏洞

**v2 设计**(§1.6):"operation_id 由 (recommendation_id + target_parameter_set_id) 哈希"

**问题**:
- 同一 rec 在 approve 和 release 之间 target_parameter_set 可以被 supersede 替换,重放时新旧 target_parameter_set_id 不同,会生成新 operation_id → Step 1 重做但不幂等(两次 UPSERT active_parameter_sets,第二次覆盖第一次)
- saga 本意是"同一次 apply 操作重放不影响",不应跟 target_parameter_set_id 绑定

**建议**:
- operation_id = UUID4 在第一次调用时生成并写入 `parameter_apply_history`,后续重试从 `parameter_apply_history` 读同一 operation_id 继续
- `/apply` endpoint 返回 operation_id,客户端重试带这个 id
- 或者:operation_id 由 `(recommendation_id + apply_token_nonce)` 组成,确保同一 token 重用只生成一个 id

## R2-09 ⚠ Phase 2 的 scope='cost_model' 用法矛盾

**v2 设计**(§0.2 + §2.2):"scope='cost_model' 时 family/timeframe 都要填;scope='cost_model' 仅做 source 标记"

**问题**:
- 既然 cost calibration 写的是 combo-level fee / slippage,scope 留着 'combo' + `review_notes` 带 source 就够
- 多一个 'cost_model' scope 会让 Hero 顶带 / Dashboard / API list 都要处理这个亚型,复杂度↑
- CHECK 约束里 'cost_model' 和 'combo' 的字段要求一样,实质上是语义副本

**建议**:
- 删 'cost_model' scope,只用 `review_notes.source='cost_calibration'`
- Hero / UI 想区分时按 `review_notes` JSON 里的 `source` 过滤
- `VALID_SCOPES` 减到 4 个(combo/profile/sleeve/risk)

## R2-10 ❌ VALID_REC_TYPES 新增 keep_active 系列命名冲突

**v2 设计**(§0.3):新增 `profile_keep_active` + `sleeve_budget_keep`

**问题**:
- 既有 `keep_active` 是 combo scope 的"no change" rec
- 新加 `profile_keep_active` 是一致的语义但不同 scope;命名空间分裂
- 统一治理的初衷下,同样的意思不该用两个 type

**建议**:
- 只用 `keep_active`(对所有 scope 都适用),scope 字段已经区分
- rec_type 是跨 scope 通用动作;scope 是被动对象。两者正交。
- 类似地:`profile_upgrade` 可以改成 `parameter_upgrade`(rec_type)+ `scope='profile'`,不再单独设
- 这样 VALID_REC_TYPES 只加 `profile_type_review` + `sleeve_budget_adjust`(动作本身不同)

## R2-11 ❌ Hero "pending_review" 合并 combo+profile 不反映优先级

**v2 设计**(§1.10):"pending_review(combo+profile 合计)"

**问题**:
- combo rec 是 weekly 产出常规优化,profile rec 是"profile 级大动作"——业务风险/紧急程度差很大
- 合并数字会让 operator 无法直观看到 profile 级积压
- 已经有 `pending_type_review` 了,为什么不也拆 `pending_profile_review`?

**建议**:
- Hero 拆:`pending_combo` / `pending_profile` / `pending_sleeve` / `pending_type_review` 四栏
- 点击各自跳对应列表
- 保 UI 信息密度:全折叠成 `待审批 X(combo/profile/sleeve)` 的下钻气泡

## R2-12 ⚠ profile apply saga Step 1 与既有 combo upsert 路径的竞争

**v2 设计**(§1.1):`uq_active_combo` 改成 partial unique index (WHERE scope='combo')

**问题**:
- 既有 `aats/data_platform/governance/active_parameter_sets_db.py` 的 UPSERT 走 `ON CONFLICT (family, timeframe) DO UPDATE`——partial unique index 没有跟随的 named constraint,`ON CONFLICT` 需要 **constraint name** 或 **column list**
- `ON CONFLICT (family, timeframe)` 在有 partial index 的表上是**允许**的,但 Postgres 只在 partial index 谓词满足时才触发 → 对 `scope='combo'` 的新插入 OK,但如果 RDP daemon bug 把 scope 写成了别的值,ON CONFLICT 不拦

**建议**:
- v1 的 `ON CONFLICT (family, timeframe)` 要改成 `ON CONFLICT (family, timeframe) WHERE scope = 'combo'` (显式 partial)
- 对 scope='profile' 的 upsert 单独写一条 `ON CONFLICT (scope_ref) WHERE scope = 'profile'`

## R2-13 ⚠ decision_mid_price 契约 PR 未定义

**v2 设计**(§2.2.1):"Phase 2 上线前先打一个小 PR 往 execution 路径注入 `decision_mid_price`"

**问题**:
- decision_mid_price 应该在 **哪一步** 写?下单前?成交回报时?
- 谁提供 mid?market_gateway 订阅的 level1 tick?
- 写进 raw_payload 的时序:如果在下单后才写,fills 订阅 race 可能读不到
- 这个契约 PR 估工量、owner、排期都没

**建议**:
- 契约 PR 的 spec 要进 v3 设计的附录:
  - 写入点:execution_control `prepare_order()` 完成阶段,mid 取自 market_gateway 的最新 tick
  - 字段名:`raw_payload.decision.mid_price_at_decision`(嵌套防顶层膨胀)
  - 必须先上线契约 PR,再上 Phase 2 research job(否则 research 跑空样本)

## R2-14 ❌ sleeve scope 的 rec 禁 apply,但没禁 approve/release

**v2 设计**(§3.3):"scope='sleeve' 的 rec 在 /apply 返回 403"

**问题**:
- approve 允许,release 允许,apply 403 —— 释放了 approved+released 但不 apply 的"悬挂 release"
- observation-only 的 UX 应该是:**不存在 approve / release 按钮**,只有"mark reviewed"
- 既然 approve/release 对 sleeve 毫无作用,让它们可调用反而会让 operator 以为"我已经做了流程"

**建议**:
- sleeve scope 的 rec,approve/release/apply 端点都拒绝(403 "observation-only")
- UI 只展示 "Mark as Reviewed" 按钮
- 或统一:sleeve 走独立 table 后的 v1 架构(方案 B),这次重新评估——因 observation-only 和审批治理本质不是一回事

## R2-15 ⚠ Daemon heartbeat 写 system_config 表的副作用

**v2 设计**(§6.3):"RDP daemon 每 60s 写一次 `governance.system_config.key='rdp_daemon_heartbeat'`"

**问题**:
- system_config 本意是 feature flag / 配置,加 heartbeat 是混用 — 配置变更历史表(R2-06 建议的)会被 heartbeat 每分钟灌满
- 1000/天 × 30 天 = 3w history rows/月,纯 heartbeat 噪音

**建议**:
- heartbeat 单独放 `governance.rdp_daemon_heartbeat(singleton_key='rdp_daemon', heartbeat_at TIMESTAMPTZ)` 单行表
- 或者写 Redis(已有基础设施,pubsub 更自然)
- 不要混进 system_config

---

## 审查结论

**v2 解决了 v1 的 20 项,但引入了 15 项新问题:**

- **Blocker(❌) 7 项:** R2-01, R2-03, R2-06, R2-08, R2-10, R2-11, R2-14
- **Warning(⚠) 8 项:** R2-02(方向变化逻辑), R2-04, R2-05, R2-07, R2-09, R2-12, R2-13, R2-15

**建议:所有 Blocker 必须在 v3 优化时解决;Warning 至少解决 5 项。**

---

## v2 继承的,但 v1 未指明细节的隐藏项

- R2-16 `parameter_apply_history.operation_id` 列的现有 DDL 要求确认(v1 审查没问到)
- R2-17 `governance.parameter_releases` 表对 scope='profile' 的 release 支持(本 SOW 没碰,但 /release endpoint 依赖它)
- R2-18 `rdp_apply_token.py` HMAC 的 actor_id 字段目前是什么结构?能否支持 role_group?(R2-04 依赖)

这三项留给 v3 时一并回答。
