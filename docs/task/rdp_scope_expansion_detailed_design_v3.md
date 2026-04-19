# RDP 能力扩展 · 详细设计 v3 (Phase 1-4)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 版本: v3 — 基于 [rdp_scope_expansion_review_v2.md](./rdp_scope_expansion_review_v2.md) 的 7 blocker + 8 warning 优化
> 起草: 2026-04-18
> 本文件替代 v2,最终实施版本

---

## v2 → v3 变更摘要

| ID | v2 问题 | v3 方案 |
|----|---------|---------|
| R2-01 | Saga Step 3 payload 粗暴 merge,可能覆盖非 RDP 字段 | `jsonb_set` 只改白名单 key + Step 3 前 FOR UPDATE + baseline 校验 |
| R2-02 | streak 方向变化误重置 | 方向变化仍 ++,direction → `mixed`,review rec 区分三种方向 |
| R2-03 | batch_b_99_rollback 漏表 | 拆四个 rollback 文件,每个 batch 配对;SOP 强制逆序执行 |
| R2-04 | apply_token 只含 actor/action,可跨 rec 复用 | token v2 格式:`actor|action|scope|rec_id|exp_ts`;向后兼容保留 v1 读逻辑 |
| R2-05 | get_live_session 生命周期未定义 | eager pool + pre_ping + recycle + fail-fast |
| R2-06 | system_config 并发写无保护 | 加 version 列 CAS + system_config_history 审计表 |
| R2-07 | 27 grid × 5 profile 性能未实测 | Shadow 打 metric;若超 1500s 切 coordinate descent |
| R2-08 | operation_id 绑定 target_parameter_set_id 有漏洞 | operation_id = UUID4 在 /apply 第一次调用时生成,存 idempotent table |
| R2-09 | scope='cost_model' 与 'combo' 语义副本 | 删 'cost_model' scope,仅用 `review_notes.source='cost_calibration'` |
| R2-10 | profile_keep_active / sleeve_budget_keep 命名分裂 | 只加 `profile_type_review` + `sleeve_budget_adjust`,其他复用既有 rec_type |
| R2-11 | Hero "pending_review" 合并 combo+profile 无优先级 | Hero 拆 4 栏:pending_combo / pending_profile / pending_sleeve / pending_type_review |
| R2-12 | partial unique index + ON CONFLICT 的兼容 | 既有 UPSERT 显式加 `WHERE scope='combo'` 谓词 |
| R2-13 | decision_mid_price 契约 PR 未定义 | v3 附录 A 给出完整契约 spec |
| R2-14 | sleeve rec 只禁 apply,不禁 approve/release | sleeve scope 所有写操作端点 403,UI 只留"Mark Reviewed" |
| R2-15 | daemon heartbeat 混进 system_config | 新独立表 `governance.rdp_daemon_heartbeat` 单行 + 写 Redis 双通道 |

---

## 0. 全局约定(v3)

### 0.1 不变部分

同 v2 §0.1。

### 0.2 Scope 枚举(v3 精简)

```python
# aats/data_platform/governance/_db_util.py
VALID_SCOPES = frozenset({
    "combo",      # family × timeframe 组合参数(含 cost calibration 产出)
    "profile",    # Phase 1:strategy profile 级阈值
    "sleeve",     # Phase 3:sleeve budget
    "risk",       # Phase 4:占位
})
# 注:v2 的 'cost_model' 已删;cost calibration 源信息靠 review_notes.source 标记
```

### 0.3 VALID_REC_TYPES(v3 精简,R2-10)

```python
VALID_REC_TYPES = frozenset({
    # 既有,跨 scope 通用
    "parameter_upgrade",  # 任何 scope 的参数调整
    "keep_active",        # 任何 scope 的 no-change 结论
    "lower_priority",
    "pause",
    "require_review",
    # v3 新增(2 个,真正语义不同)
    "profile_type_review",   # 只有 profile scope 用:连续 3 轮 clamp 超界
    "sleeve_budget_adjust",  # 只有 sleeve scope 用:budget 建议变更
})
```

**消费端适配:** `recommendations_db.py:62` 错误消息里 `sorted(VALID_REC_TYPES)` 的断言改用子集白名单:
```python
assert {"parameter_upgrade", "keep_active"}.issubset(VALID_REC_TYPES)
```

### 0.4 apply_token v2 格式(R2-04)

**v2 payload 扩展**(向后兼容):

```
v1 format: {actor}|{action}|{exp_ts}                   # 现有,cost calibration 继续可用
v2 format: {actor}|{action}|{scope}|{rec_id}|{exp_ts}  # Phase 1 起新签发
```

解析时先按 `|` 分段计数:
- 3 段 → v1 token(`scope='combo'` + `rec_id=*`(通配,仅限 scope='combo'))
- 5 段 → v2 token(必须 token.rec_id == URL.rec_id)

`scope='profile'` / `'sleeve'` 的 apply **只接受 v2 token**。

**双签实现(profile / sleeve):**

```python
# /apply handler 检查
if rec.scope in ('profile', 'sleeve'):
    approver_actor = rec.approved_by
    applier_actor  = token.actor
    if approver_actor == applier_actor:
        raise HTTPException(403, "double-sign: approver ≠ applier required")
    # 不检查 role_group(新增字段工作量大),只保证 actor 不同
```

---

## Phase 1:Profile-level 参数纳管(v3)

### 1.1 数据模型(v3 修正 R2-01/02/03/06/12/15)

#### Migration `batch_b_01_core_schema.sql`

(仅核心 scope 扩展,system_config,heartbeat)

```sql
BEGIN;

-- A. recommendations / parameter_sets / active_parameter_sets / parameter_apply_history
--    加 scope + scope_ref + CHECK(同 v2 §1.1,删 cost_model)

ALTER TABLE governance.recommendations
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_rec_scope_ref
    ON governance.recommendations(scope, scope_ref, status);

ALTER TABLE governance.recommendations
    DROP CONSTRAINT IF EXISTS chk_rec_scope;
ALTER TABLE governance.recommendations
    ADD CONSTRAINT chk_rec_scope CHECK (
        scope IN ('combo', 'profile', 'sleeve', 'risk')
    );

ALTER TABLE governance.recommendations
    DROP CONSTRAINT IF EXISTS chk_rec_scope_fields;
ALTER TABLE governance.recommendations
    ADD CONSTRAINT chk_rec_scope_fields CHECK (
        CASE
          WHEN scope = 'combo'         THEN family IS NOT NULL AND timeframe IS NOT NULL
          WHEN scope IN ('profile', 'sleeve') THEN scope_ref IS NOT NULL
          ELSE TRUE
        END
    );

ALTER TABLE governance.parameter_sets
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

ALTER TABLE governance.parameter_sets
    ALTER COLUMN family DROP NOT NULL,
    ALTER COLUMN timeframe DROP NOT NULL;

ALTER TABLE governance.parameter_sets
    DROP CONSTRAINT IF EXISTS chk_ps_scope_fields;
ALTER TABLE governance.parameter_sets
    ADD CONSTRAINT chk_ps_scope_fields CHECK (
        CASE
          WHEN scope = 'combo'         THEN family IS NOT NULL AND timeframe IS NOT NULL
          WHEN scope IN ('profile', 'sleeve') THEN scope_ref IS NOT NULL
          ELSE TRUE
        END
    );

ALTER TABLE governance.active_parameter_sets
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

ALTER TABLE governance.active_parameter_sets
    ALTER COLUMN family DROP NOT NULL,
    ALTER COLUMN timeframe DROP NOT NULL;

ALTER TABLE governance.active_parameter_sets
    DROP CONSTRAINT IF EXISTS uq_active_combo;

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_combo
    ON governance.active_parameter_sets(family, timeframe)
    WHERE scope = 'combo';
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_profile
    ON governance.active_parameter_sets(scope_ref)
    WHERE scope = 'profile';
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_sleeve
    ON governance.active_parameter_sets(scope_ref)
    WHERE scope = 'sleeve';

ALTER TABLE governance.parameter_apply_history
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_apply_history_scope_ref
    ON governance.parameter_apply_history(scope, scope_ref, created_at DESC);

-- B. system_config — R2-06 加 version + 审计
CREATE TABLE IF NOT EXISTS governance.system_config (
    key          VARCHAR(128) PRIMARY KEY,
    value        JSONB        NOT NULL,
    version      INTEGER      NOT NULL DEFAULT 1,
    updated_by   VARCHAR(128) NOT NULL,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS governance.system_config_history (
    id           BIGSERIAL PRIMARY KEY,
    key          VARCHAR(128) NOT NULL,
    old_value    JSONB,
    new_value    JSONB        NOT NULL,
    old_version  INTEGER,
    new_version  INTEGER      NOT NULL,
    changed_by   VARCHAR(128) NOT NULL,
    changed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_sc_history_key_time
    ON governance.system_config_history(key, changed_at DESC);

INSERT INTO governance.system_config(key, value, version, updated_by, notes) VALUES
    ('profile_upgrade_auto_apply_enabled', 'false'::jsonb, 1, 'migration', 'Phase 1 shadow flag'),
    ('cost_calibration_auto_recommend_enabled', 'false'::jsonb, 1, 'migration', 'Phase 2 shadow flag'),
    ('sleeve_budget_advice_enabled', 'true'::jsonb, 1, 'migration', 'Phase 3 observation-only 默认开')
ON CONFLICT (key) DO NOTHING;

-- C. rdp_daemon_heartbeat — R2-15 独立单行表
CREATE TABLE IF NOT EXISTS governance.rdp_daemon_heartbeat (
    singleton_key VARCHAR(32) PRIMARY KEY CHECK (singleton_key = 'rdp_daemon'),
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pid           INTEGER,
    version       VARCHAR(64)
);

INSERT INTO governance.rdp_daemon_heartbeat(singleton_key)
VALUES ('rdp_daemon') ON CONFLICT DO NOTHING;

-- D. apply saga idempotency — R2-08
CREATE TABLE IF NOT EXISTS governance.apply_saga_operations (
    operation_id         VARCHAR(64) PRIMARY KEY,  -- UUID4
    recommendation_id    VARCHAR(128) NOT NULL,
    scope                VARCHAR(32) NOT NULL,
    step1_done_at        TIMESTAMPTZ,
    step2_done_at        TIMESTAMPTZ,
    step3_done_at        TIMESTAMPTZ,
    step4_done_at        TIMESTAMPTZ,
    last_error           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor                VARCHAR(128) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_saga_op_rec
    ON governance.apply_saga_operations(recommendation_id, created_at DESC);

COMMIT;
```

#### Migration `batch_b_01_rollback.sql`(R2-03)

```sql
BEGIN;
-- 保留 parameter_apply_history 审计字段;其他新增结构全删
DROP TABLE IF EXISTS governance.apply_saga_operations;
DROP TABLE IF EXISTS governance.rdp_daemon_heartbeat;
DROP TABLE IF EXISTS governance.system_config_history;
DROP TABLE IF EXISTS governance.system_config;

DROP INDEX IF EXISTS governance.uq_active_sleeve;
DROP INDEX IF EXISTS governance.uq_active_profile;
DROP INDEX IF EXISTS governance.uq_active_combo;

ALTER TABLE governance.active_parameter_sets
    ALTER COLUMN family SET NOT NULL,
    ALTER COLUMN timeframe SET NOT NULL;
ALTER TABLE governance.active_parameter_sets
    ADD CONSTRAINT uq_active_combo UNIQUE (family, timeframe);

ALTER TABLE governance.recommendations DROP CONSTRAINT IF EXISTS chk_rec_scope_fields;
ALTER TABLE governance.recommendations DROP CONSTRAINT IF EXISTS chk_rec_scope;
ALTER TABLE governance.parameter_sets DROP CONSTRAINT IF EXISTS chk_ps_scope_fields;
DROP INDEX IF EXISTS governance.ix_rec_scope_ref;
DROP INDEX IF EXISTS governance.ix_apply_history_scope_ref;
COMMIT;
```

#### Migration `batch_b_02_profile_research.sql`

同 v2(profile_research_runs + profile_type_review_streak)。streak 表加约束:

```sql
ALTER TABLE governance.profile_type_review_streak
    ADD CONSTRAINT chk_streak_direction CHECK (
        clamp_violation_direction IN ('above_upper', 'below_lower', 'mixed')
    );
```

#### Migration `batch_b_02_rollback.sql`

```sql
BEGIN;
DROP INDEX IF EXISTS governance.ix_profile_research_profile_started;
DROP TABLE IF EXISTS governance.profile_type_review_streak;
DROP TABLE IF EXISTS governance.profile_research_runs;
COMMIT;
```

#### Migration `batch_b_03_cost_calibration.sql` / `batch_b_03_rollback.sql`

表同 v2 §2.1;rollback 对称。

#### Migration `batch_b_04_sleeve_advice.sql` / `batch_b_04_rollback.sql`

只建视图(见 §3.1);rollback `DROP VIEW`。

### 1.2 Research Job(v3 修正 R2-02/07)

**Streak 方向变化逻辑(R2-02)** — CAS 表达式更新:

```sql
-- 替换 v2 的 CAS
INSERT INTO governance.profile_type_review_streak
    (profile_id, clamp_violation_direction, streak_count,
     last_run_id, last_updated)
VALUES
    (:pid, :dir, 1, :run_id, NOW())
ON CONFLICT (profile_id) DO UPDATE SET
    streak_count = CASE
        -- 相同方向 + 新 run:++
        WHEN governance.profile_type_review_streak.clamp_violation_direction = EXCLUDED.clamp_violation_direction
         AND governance.profile_type_review_streak.last_run_id != EXCLUDED.last_run_id
        THEN governance.profile_type_review_streak.streak_count + 1
        -- 方向变化:依然 ++,但记作 mixed(关键修正 R2-02)
        WHEN governance.profile_type_review_streak.clamp_violation_direction != EXCLUDED.clamp_violation_direction
         AND governance.profile_type_review_streak.last_run_id != EXCLUDED.last_run_id
        THEN governance.profile_type_review_streak.streak_count + 1
        -- 同一 run 重放:no-op
        ELSE governance.profile_type_review_streak.streak_count
    END,
    clamp_violation_direction = CASE
        WHEN governance.profile_type_review_streak.clamp_violation_direction != EXCLUDED.clamp_violation_direction
         AND governance.profile_type_review_streak.last_run_id != EXCLUDED.last_run_id
        THEN 'mixed'
        ELSE EXCLUDED.clamp_violation_direction
    END,
    last_run_id = EXCLUDED.last_run_id,
    last_updated = NOW()
RETURNING streak_count, clamp_violation_direction
```

**Grid 性能 observability(R2-07):**

```python
# profile_research_job.py 里加 metric
PROFILE_RESEARCH_DURATION_S = Histogram(
    "rdp_profile_research_duration_seconds",
    "Profile research job runtime",
    ["profile_id", "grid_method", "grid_size"],
)

# 每次 run 结束记录
PROFILE_RESEARCH_DURATION_S.labels(
    profile_id=profile_id,
    grid_method=grid_method,
    grid_size=grid_size,
).observe(duration_s)
```

Shadow 期观察 P95;若超 1500s 切 coordinate descent(每轮只动一维,3 轮完成)。

### 1.3 Recommendation 扩展

同 v2,`scope='profile'`,`family=NULL`,`timeframe=NULL`。

### 1.4 Gate 规则

不变(同 v1/v2)。

### 1.5 API 端点(v3 修正 R2-04/14)

**Profile 路径:**

| 方法 | 路径 | token v2 required |
|------|------|-------|
| GET | `/rdp/profile-recommendations` | — |
| GET | `/rdp/profile-recommendations/{id}` | — |
| POST | `/rdp/profile-recommendations/{id}/approve` | ✓(action=approve) |
| POST | `/rdp/profile-recommendations/{id}/reject` | ✓(action=reject) |
| POST | `/rdp/profile-recommendations/{id}/gate` | — |
| POST | `/rdp/profile-recommendations/{id}/release` | ✓(action=release) |
| POST | `/rdp/profile-recommendations/{id}/apply` | ✓✓(action=apply + approver≠applier) |
| POST | `/rdp/profile-recommendations/{id}/rollback` | ✓✓(action=rollback + approver≠applier) |
| GET | `/rdp/profile-type-reviews` | — |
| POST | `/rdp/profile-type-reviews/{id}/resolve` | ✓(action=review_resolve) |

**Sleeve 路径(R2-14 修正)**:

| 方法 | 路径 | 行为 |
|------|------|------|
| GET | `/rdp/sleeve-advice/recent` | 列表 |
| POST | `/rdp/sleeve-advice/{id}/mark-reviewed` | 仅 UI 标记 |
| POST | `/rdp/sleeve-advice/{id}/approve` | **403** "observation-only" |
| POST | `/rdp/sleeve-advice/{id}/release` | **403** "observation-only" |
| POST | `/rdp/sleeve-advice/{id}/apply` | **403** "observation-only" |

### 1.6 跨库 Apply Saga(v3 修正 R2-01/08)

#### 1.6.1 Operation 生命周期(R2-08)

```python
# /apply 的 handler 总流程
def apply_profile(recommendation_id: str, token: str, actor: str):
    # Step 0: 找或建 operation(以 recommendation_id 为查询键)
    saga_op = find_or_create_saga_operation(
        session=research_session,
        recommendation_id=recommendation_id,
        scope='profile',
        actor=actor,
    )
    # saga_op.operation_id 是 UUID4,第一次调用时生成,重试时复用
    # 重试时如果 saga_op.stepN_done_at 已有值,相应 step 直接跳过

    return apply_profile_saga(
        research_session=research_session,
        live_session=get_live_session(mode='rw'),
        saga_op=saga_op,
        target_parameter_set_id=load_target_ps(recommendation_id),
    )
```

#### 1.6.2 Step 3 的 safe merge(R2-01)

```python
def step3_update_live_payload(
    live_session: Session,
    *,
    profile_id: str,
    threshold_patches: dict[str, dict[str, float]],  # {"key": {"from": x, "to": y}}
    operation_id: str,
) -> None:
    """Step 3: 合并 threshold_patches 到 strategy_profile_activation.payload。

    安全措施:
      1. SELECT FOR UPDATE 目标行(单行锁)
      2. 对每个 patch key:校验 payload[key] == patches[key]["from"](baseline 一致)
         如果不一致 → raise StepThreeBaselineDriftError
      3. 用 jsonb_set 只改白名单 key,其他字段不动
      4. 写 strategy_profile_activation_history

    白名单(hard-coded,防泛化):
      ['strategy_entry_min_signal_edge_bps',
       'strategy_entry_alpha_min',
       'strategy_min_net_edge_bps']
    """
    WHITELIST = {
        "strategy_entry_min_signal_edge_bps",
        "strategy_entry_alpha_min",
        "strategy_min_net_edge_bps",
    }

    for key in threshold_patches:
        if key not in WHITELIST:
            raise ValueError(f"patch key {key!r} not in whitelist")

    # SELECT FOR UPDATE
    row = live_session.execute(text("""
        SELECT activation_id, payload
        FROM strategy_profile_activation
        WHERE payload->>'profile_id' = :pid
        FOR UPDATE
    """), {"pid": profile_id}).first()

    if row is None:
        raise StepThreeTargetNotFoundError(profile_id)

    payload = row.payload
    for key, patch in threshold_patches.items():
        current = payload.get(key)
        if current != patch["from"]:
            raise StepThreeBaselineDriftError(
                f"profile {profile_id} key {key}: "
                f"live={current} != expected_from={patch['from']}"
            )

    # jsonb_set 逐键
    for key, patch in threshold_patches.items():
        live_session.execute(text("""
            UPDATE strategy_profile_activation
            SET payload = jsonb_set(payload, ARRAY[:key], to_jsonb(:val::numeric))
            WHERE activation_id = :aid
        """), {"aid": row.activation_id, "key": key, "val": patch["to"]})

    # 写 history
    live_session.execute(text("""
        INSERT INTO strategy_profile_activation_history
            (activation_event_id, product_type, margin_mode, executed_at, payload)
        VALUES (:eid, :pt, :mm, NOW(), :pl)
    """), {...})  # 省略 args
    live_session.commit()
```

#### 1.6.3 Reconcile 脚本

`scripts/rdp_apply_saga_reconcile.py`:
- 扫 `apply_saga_operations WHERE step1_done_at IS NOT NULL AND step4_done_at IS NULL AND created_at < NOW() - INTERVAL '5 minutes'`
- 对每个 drift op:打告警 + 尝试续跑失败的 step

### 1.7 Cross-DB 连接(v3 修正 R2-05)

```python
# aats/data_platform/runtime/session.py

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_live_engine_rw = None
_live_engine_ro = None

def init_live_engines() -> None:
    """在 RDP daemon / API 进程启动时 eager 调用;失败则进程不启动(fail-fast)。"""
    global _live_engine_rw, _live_engine_ro

    url_rw = os.environ.get("AATS_LIVE_DB_URL_RDP")
    if not url_rw:
        raise RuntimeError("AATS_LIVE_DB_URL_RDP not set")

    _live_engine_rw = create_engine(
        url_rw,
        pool_size=3, max_overflow=2,
        pool_recycle=300,
        pool_pre_ping=True,
        pool_timeout=30,
    )
    # RO 复用同一 URL,但在 connection event 里设置 default_transaction_read_only
    _live_engine_ro = create_engine(
        url_rw,
        pool_size=2, max_overflow=2,
        pool_recycle=300,
        pool_pre_ping=True,
        pool_timeout=30,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    # Eager ping
    with _live_engine_rw.connect() as c:
        c.execute(text("SELECT 1"))
    with _live_engine_ro.connect() as c:
        c.execute(text("SELECT 1"))

def get_live_session(mode: str = "rw"):
    """返回新 session;调用方必须 close/commit。"""
    engine = _live_engine_rw if mode == "rw" else _live_engine_ro
    if engine is None:
        raise RuntimeError("call init_live_engines() first")
    return sessionmaker(bind=engine, expire_on_commit=False)()
```

### 1.8 Shadow 期(v3 修正 R2-06 — CAS flag write)

```python
# system_config write API
@app.post("/rdp/system-config/{key}")
def update_config(key: str, body: UpdateBody, token: ApplyToken):
    # CAS write
    result = session.execute(text("""
        UPDATE governance.system_config
        SET value = :new_val,
            version = version + 1,
            updated_by = :actor,
            updated_at = NOW()
        WHERE key = :key AND version = :expected_version
        RETURNING version
    """), {
        "key": key,
        "new_val": body.value,
        "actor": token.actor,
        "expected_version": body.version,
    })
    if result.rowcount == 0:
        raise HTTPException(409, "version conflict, reread and retry")

    # 写 history
    session.execute(text("""
        INSERT INTO governance.system_config_history
            (key, old_value, new_value, old_version, new_version, changed_by)
        VALUES (:k, :ov, :nv, :oversion, :nversion, :actor)
    """), {...})
    session.commit()
```

### 1.9 回滚

同 v2(运行时 rollback 通过 `/rollback` endpoint 走同 saga)。

### 1.10 UI(v3 修正 R2-11)

**Hero 顶带(四栏拆分 + 下钻):**

```
┌─ RDP Hero ────────────────────────────────────────────────────┐
│ 待审批: 5 (combo 3 / profile 2 / sleeve 0)                     │
│ 待人工审查: 1 (profile_type_review)                             │
│ 观察中: 3                                                       │
│ 阻断: 0                                                         │
│ 队列: 2                                                         │
└───────────────────────────────────────────────────────────────┘
```

实现:`aats/api/rdp_control_summary.py` 聚合查询按 scope GROUP BY,前端渲染 "总数 (细分)"。

### 1.11 测试(v3 新增)

在 v2 测试基础上增加:

| 新测试 | 覆盖 |
|--------|------|
| `test_step3_baseline_drift_aborts.py` | live payload 不是 expected_from 时 Step 3 抛 + saga 状态标 failed |
| `test_step3_jsonb_set_whitelist.py` | 非白名单 key 被拒;其他 payload 字段不动 |
| `test_streak_direction_mixed.py` | 方向变化 → direction='mixed' 且 streak++ |
| `test_apply_token_v2_scope_binding.py` | v2 token rec_id 不匹配 URL → 403 |
| `test_apply_token_v1_backcompat.py` | v1 token(3段)仍可用于 scope='combo' |
| `test_apply_double_sign_enforcement.py` | approver==applier → 403 |
| `test_system_config_cas_conflict.py` | 并发 flip → 一胜一败(409) |
| `test_saga_operation_id_uuid.py` | 重试时 operation_id 复用,不会生成新 row |
| `test_hero_scope_breakdown.py` | Hero 4 栏数字正确 |

---

## Phase 2:Cost Model 校准(v3 修正 R2-09/13)

### 2.1 数据模型

同 v2 §2.1(`governance.cost_calibration_runs` 独立表,但产出 rec 的 `scope='combo'`,`review_notes.source='cost_calibration'`)。

### 2.2 Research Job

- 跨库读用 `get_live_session(mode='ro')`
- decision_price fallback 同 v2 §2.2(raw_payload['decision']['mid_price_at_decision'] → limit_price → None 排除)
- 产出 rec 用 `scope='combo'` + 特征化 `review_notes`

### 2.3 decision_mid_price 契约(v3 附录 A,R2-13)

见本文件末尾 **附录 A**。

### 2.4-2.6 API / UI / Guardrail / 测试

同 v2。

---

## Phase 3:Sleeve Budget Advice(v3 修正 R2-14)

### 3.1 模型

同 v2 §3.1(无独立表,只建 `vw_sleeve_advice_recent` 视图)。

### 3.2 Research Job

同 v2。

### 3.3 API

**所有** sleeve scope 的写端点(approve / release / apply)返回 403。
只暴露 GET + `/mark-reviewed`。

### 3.4 UI / Guardrail / 测试

UI 只渲染 "Mark as Reviewed" 按钮,没有 Approve/Reject/Release/Apply。
测试 `test_sleeve_all_writes_return_403.py` 覆盖四个端点。

---

## Phase 4:Risk Guardrail(占位)

不改。

---

## 5. 跨 Phase 依赖图(v3)

同 v2 §5。

---

## 6. Observability(v3 修正 R2-15)

**Heartbeat 双通道:**

- 主:`governance.rdp_daemon_heartbeat(singleton_key='rdp_daemon')` 每 60s UPDATE
- 辅:Redis `SET aats:rdp:heartbeat <timestamp> EX 120` 每 60s

Grafana alert 检查:
- `rdp_daemon_heartbeat.heartbeat_at < NOW() - INTERVAL '10 minutes'` OR Redis key 缺失 → 告警

其他 alerts 同 v2 §6.2。

---

## 7. Deployment 顺序

沿用 v2 §7。

`aats/data_platform/migrations/_batch_b.py` 加:

```python
BATCH_B_SQL_FILES = [
    "batch_b_01_core_schema.sql",
    "batch_b_02_profile_research.sql",
    "batch_b_03_cost_calibration.sql",
    "batch_b_04_sleeve_advice.sql",
]

BATCH_B_ROLLBACK_FILES = [
    "batch_b_04_rollback.sql",
    "batch_b_03_rollback.sql",
    "batch_b_02_rollback.sql",
    "batch_b_01_rollback.sql",
]
```

---

## 8. Review Checkpoints(v3)

Phase 1 上线前必检:

- [ ] `batch_b_01_core_schema.sql` + `batch_b_01_rollback.sql` 在复制库 roundtrip 通过
- [ ] `uq_active_combo` drop 后既有 combo UPSERT 走 `ON CONFLICT (family, timeframe) WHERE scope = 'combo'` 仍正常工作
- [ ] `system_config` CAS write 测试覆盖 version 冲突 409
- [ ] `init_live_engines()` 在 daemon 启动时 fail-fast
- [ ] `apply_token v2` 向后兼容 v1(只 scope='combo')
- [ ] 双签:approver ≠ applier 强制
- [ ] Step 3 baseline drift 告警链路通(告警→reconcile 脚本)
- [ ] streak direction='mixed' 分支有 test
- [ ] Hero 4 栏下钻 UI 完成
- [ ] `rdp_daemon_heartbeat` 表 + Redis 双写完成,alert 规则 load

Phase 2 上线前必检:

- [ ] execution 服务已写 `raw_payload.decision.mid_price_at_decision`(契约 PR 合入)
- [ ] cost_calibration 读 live DB 7 天回填验证
- [ ] slippage 方向测试:买入单价高于 mid = 正 slip;卖出单价低于 mid = 正 slip

Phase 3 上线前必检:

- [ ] sleeve scope 所有写端点(approve/release/apply)返回 403
- [ ] UI 无 Approve/Reject/Release/Apply 按钮
- [ ] `vw_sleeve_advice_recent` 视图对既有 SELECT 无影响

---

## 9. Blocker / Warning 解决状态(累计 v2+v3)

### v1 Blocker / Warning(v2 已全解,v3 继承)

全部保持 resolved 状态。

### v2 Blocker / Warning(v3 解决)

| ID | 状态 |
|----|------|
| R2-01 | ✅ jsonb_set + FOR UPDATE + baseline 校验 |
| R2-02 | ✅ direction='mixed' + streak++ |
| R2-03 | ✅ 分文件 rollback + SOP 强制逆序 |
| R2-04 | ✅ apply_token v2 格式 + rec_id 绑定 + 双签 |
| R2-05 | ✅ eager pool + pre_ping + fail-fast init |
| R2-06 | ✅ version CAS + system_config_history |
| R2-07 | ✅ Prometheus histogram + coordinate descent 备选 |
| R2-08 | ✅ UUID4 operation_id + apply_saga_operations 表 |
| R2-09 | ✅ 删 cost_model scope,仅 review_notes 标记 |
| R2-10 | ✅ 只加 2 个新 rec_type,复用既有 |
| R2-11 | ✅ Hero 4 栏下钻 |
| R2-12 | ✅ 显式 ON CONFLICT ... WHERE scope='combo' |
| R2-13 | ✅ 附录 A 给出完整契约 |
| R2-14 | ✅ sleeve 所有写端点 403 |
| R2-15 | ✅ 独立 heartbeat 表 + Redis 双通道 |

**v3 15 项全部 resolved;v1 20 项 + v2 15 项 = 35 项综合解决。**

---

## 附录 A:Phase 2 先决契约 PR · decision_mid_price

### A.1 写入点

`aats/services/execution_control/order_service.py` 的 `prepare_order()` 完成阶段(下单前):

```python
decision_mid = market_gateway_client.get_latest_mid(symbol=symbol)
if decision_mid is not None:
    order.raw_payload.setdefault("decision", {})
    order.raw_payload["decision"]["mid_price_at_decision"] = float(decision_mid)
    order.raw_payload["decision"]["mid_price_ts"] = datetime.utcnow().isoformat()
```

### A.2 字段 schema

- `raw_payload.decision.mid_price_at_decision: float | None`  —  best mid 价,买卖均价
- `raw_payload.decision.mid_price_ts: str (ISO8601)` —  取 mid 的时刻 UTC

### A.3 数据源

`market_gateway_client.get_latest_mid(symbol)`:
- 订阅 okx level1 tick 的内存缓存
- 返回 (bid+ask)/2 的最新值
- 缺失(tick stale > 5s) → 返回 None,不阻塞下单,但 cost calibration 这条 fill 从 slippage 样本排除

### A.4 契约 PR 范围(单独的小 PR)

- 修改 `aats/services/execution_control/order_service.py::prepare_order`
- 修改 `aats/services/market_gateway/client.py::get_latest_mid`(若不存在)
- 单测 `test_decision_mid_price_written.py`
- **不改** cost calibration 侧代码(仍在 Phase 2 本体)

### A.5 Rollout

契约 PR 合入后观察 1 周,确保:
- 新产出的 orders 都含 `decision.mid_price_at_decision`
- tick stale 比例 < 2%

通过后才合入 Phase 2 cost calibration 代码。

### A.6 工作量

预估 1-2 天(1 个 AI 工时),由 Phase 2 kickoff 时独立 PR 提交。
