# RDP 能力扩展 · 详细设计 v2 (Phase 1-4)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 版本: v2 — 基于 [rdp_scope_expansion_review_v1.md](./rdp_scope_expansion_review_v1.md) 的 10 blocker + 9 warning 优化而来
> 起草: 2026-04-18
> 本文件替代 [rdp_scope_expansion_detailed_design.md](./rdp_scope_expansion_detailed_design.md) v1

---

## 变更摘要(与 v1 对比)

| ID | v1 问题 | v2 方案 |
|----|---------|---------|
| R1-01 | `DROP INDEX uq_active_combo` 错(实际是 CONSTRAINT) | 改 `ALTER TABLE ... DROP CONSTRAINT`,重建为 partial unique index |
| R1-02/03 | `family='_profile'` hack + timeframe NOT NULL | `family` 和 `timeframe` 改 NULLABLE,用 scope 区分;加 CHECK (scope 组合合法) |
| R1-06 | feature flag 位置不明 | 新建 `governance.system_config` KV 表,API 走 apply_token |
| R1-07 | apply_token action 冲突 | 复用 `action=apply`,服务端根据 scope 决定是否要求双签 |
| R1-09 | 跨库访问 fills | 新 env `AATS_LIVE_DB_URL_RO`,RDP 开独立 read-only pool |
| R1-11 | `parameter_apply_history` 缺 scope | 同批次 batch_b_01 加 `scope` + `scope_ref` |
| R1-12 | `VALID_REC_TYPES` 变化破测试 | 全局 grep 清单 + `VALID_REC_TYPES_V2` 分文件配置,测试拆 |
| R1-17 | sleeve_budget_advice 游离治理 | 选方案 A,统一走 `recommendations` 表(scope='sleeve') |
| R1-20 | Live DB `strategy_profile_activation` 同步缺 | `apply` 走跨库 saga;定义 4 步 + 幂等键 + 补偿 |
| R1-04 | clamp 无 single source | 提取 `get_profile_clamps(profile_id)` 作为唯一源 |
| R1-05 | 200 grid points × N profiles 超时 | 降到 27 (3×3×3) + coordinate descent 选项 |
| R1-08 | streak 自增竞争 | UPDATE ... WHERE last_run_id != :new_run_id 原子 CAS |
| R1-13 | Hero 顶带未整合 | profile_upgrade 进"待审批",profile_type_review 新增"待人工审查 P" |
| R1-14 | migration runner 模式 | 沿用 batch_a 模式:`_batch_b.py` 管 Python 侧 + `batch_b_NN_*.sql` |
| R1-15 | decision_price 字段模糊 | 明确:`execution_orders.raw_payload['decision_mid_price']`(市价)或 `limit_price`(限价) |
| R1-18 | daemon 健康告警不足 | 加 3 条告警(workflow failure rate / heartbeat / queue backlog) |
| R1-19 | migration 无 rollback | batch_b_01..04 每个配 batch_b_99_rollback.sql,部署 SOP 强制 |

剩下 Warning R1-16(profile_research 每轮产 keep_active)采纳,已写入 §1.2。

---

## 0. 全局约定(v2)

### 0.1 不变

- Schema 改动走 `batch_b_NN_*.sql`,不动 batch_a
- 所有写路径经 `apply_token` HMAC 校验
- 新表加 FK 到 `governance.recommendations` / `parameter_releases`
- 所有 API 走 `aats/api/rdp_routes.py` 既有 Session + 限流

### 0.2 Scope 枚举(v2 调整)

```python
# aats/data_platform/governance/_db_util.py
VALID_SCOPES = frozenset({
    "combo",      # 现有 family × timeframe 组合参数
    "profile",    # Phase 1:strategy profile 级阈值
    "cost_model", # Phase 2:标识 source,但本质仍是 combo 写入
    "sleeve",     # Phase 3:sleeve budget
    "risk",       # Phase 4:占位,不实现
})

# scope 与 (family, timeframe) 的兼容约束
# combo      → family NOT NULL, timeframe NOT NULL
# profile    → family NULL,     timeframe NULL,     scope_ref=profile_id
# cost_model → family NOT NULL, timeframe NOT NULL  (和 combo 一致,scope 仅做源标记)
# sleeve     → family NULL,     timeframe NULL,     scope_ref=sleeve_id
# risk       → (Phase 4 时再定义,本 SOW 不实现)
```

### 0.3 新增 VALID_REC_TYPES(v2 分批合入)

**关键:R1-12 要求** — 先补齐 Python 侧,再改 SQL。

```python
VALID_REC_TYPES = frozenset({
    # 既有(不动)
    "parameter_upgrade", "keep_active", "lower_priority", "pause", "require_review",
    # Phase 1(batch_b_01 先合)
    "profile_upgrade", "profile_keep_active", "profile_type_review",
    # Phase 3(batch_b_04 再合)
    "sleeve_budget_adjust", "sleeve_budget_keep",
})
```

**升级纪律**: commit 前 `grep -rn "parameter_upgrade\|keep_active" tests/` 全部过一遍,改断言时优先改成 `set(VALID_REC_TYPES) & {固定小集}` 而不是 hardcode 全集。

---

## Phase 1:Profile-level 参数纳管(v2)

### 1.1 数据模型变更(v2 修正 Blocker R1-01/02/03/11)

#### Migration `batch_b_01_scope_columns.sql`

```sql
BEGIN;

-- 1. recommendations 加 scope + scope_ref
ALTER TABLE governance.recommendations
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_rec_scope_ref
    ON governance.recommendations(scope, scope_ref, status);

ALTER TABLE governance.recommendations
    DROP CONSTRAINT IF EXISTS chk_rec_scope;
ALTER TABLE governance.recommendations
    ADD CONSTRAINT chk_rec_scope CHECK (
        scope IN ('combo', 'profile', 'cost_model', 'sleeve', 'risk')
    );
-- scope='profile'/'sleeve' 时 family/timeframe 允许 null
-- scope='combo'/'cost_model' 时必须 non-null(保既有语义)
ALTER TABLE governance.recommendations
    DROP CONSTRAINT IF EXISTS chk_rec_scope_fields;
ALTER TABLE governance.recommendations
    ADD CONSTRAINT chk_rec_scope_fields CHECK (
        CASE
          WHEN scope IN ('combo', 'cost_model') THEN family IS NOT NULL AND timeframe IS NOT NULL
          WHEN scope IN ('profile', 'sleeve')   THEN scope_ref IS NOT NULL
          ELSE TRUE
        END
    );

-- 2. parameter_sets 同构改
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
          WHEN scope IN ('combo', 'cost_model') THEN family IS NOT NULL AND timeframe IS NOT NULL
          WHEN scope IN ('profile', 'sleeve')   THEN scope_ref IS NOT NULL
          ELSE TRUE
        END
    );

-- 3. active_parameter_sets — 核心修正(R1-01)
ALTER TABLE governance.active_parameter_sets
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

ALTER TABLE governance.active_parameter_sets
    ALTER COLUMN family DROP NOT NULL,
    ALTER COLUMN timeframe DROP NOT NULL;

-- 原来的 uq_active_combo 是 CONSTRAINT,必须用 DROP CONSTRAINT(不是 DROP INDEX)
ALTER TABLE governance.active_parameter_sets
    DROP CONSTRAINT IF EXISTS uq_active_combo;

-- 用 partial unique index 替代(scope='combo' 保留原语义)
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_combo
    ON governance.active_parameter_sets(family, timeframe)
    WHERE scope = 'combo';

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_profile
    ON governance.active_parameter_sets(scope_ref)
    WHERE scope = 'profile';

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_sleeve
    ON governance.active_parameter_sets(scope_ref)
    WHERE scope = 'sleeve';

-- 4. parameter_apply_history — R1-11 修正
ALTER TABLE governance.parameter_apply_history
    ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN IF NOT EXISTS scope_ref VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_apply_history_scope_ref
    ON governance.parameter_apply_history(scope, scope_ref, created_at DESC);

-- 5. 新建 governance.system_config(R1-06)用于 feature flag
CREATE TABLE IF NOT EXISTS governance.system_config (
    key          VARCHAR(128) PRIMARY KEY,
    value        JSONB       NOT NULL,
    updated_by   VARCHAR(128) NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes        TEXT
);

-- 种子:初始所有 shadow flag 都 false
INSERT INTO governance.system_config(key, value, updated_by, notes) VALUES
    ('profile_upgrade_auto_apply_enabled', 'false'::jsonb, 'migration', 'Phase 1 shadow flag'),
    ('cost_calibration_auto_recommend_enabled', 'false'::jsonb, 'migration', 'Phase 2 shadow flag'),
    ('sleeve_budget_advice_enabled', 'true'::jsonb, 'migration', 'Phase 3 observation-only 默认开')
ON CONFLICT (key) DO NOTHING;

COMMIT;
```

#### 回滚 `batch_b_99_rollback.sql`(R1-19)

```sql
-- 逆序 drop;注意 parameter_apply_history 的 scope 列不删(审计数据保留)
BEGIN;
DROP INDEX IF EXISTS governance.uq_active_sleeve;
DROP INDEX IF EXISTS governance.uq_active_profile;
DROP INDEX IF EXISTS governance.uq_active_combo;

ALTER TABLE governance.active_parameter_sets
    ALTER COLUMN family SET NOT NULL,
    ALTER COLUMN timeframe SET NOT NULL;
ALTER TABLE governance.active_parameter_sets
    ADD CONSTRAINT uq_active_combo UNIQUE (family, timeframe);

-- recommendations / parameter_sets 的 CHECK drop
ALTER TABLE governance.recommendations DROP CONSTRAINT IF EXISTS chk_rec_scope_fields;
ALTER TABLE governance.recommendations DROP CONSTRAINT IF EXISTS chk_rec_scope;
ALTER TABLE governance.parameter_sets DROP CONSTRAINT IF EXISTS chk_ps_scope_fields;

-- scope/scope_ref 列建议保留(有数据后删会毁审计),仅清零默认值
COMMIT;
```

#### Migration `batch_b_02_profile_research.sql`

```sql
BEGIN;

-- Profile-level research 运行记录
CREATE TABLE IF NOT EXISTS governance.profile_research_runs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(128) NOT NULL UNIQUE,
    profile_id VARCHAR(64) NOT NULL,
    oos_window_days INTEGER NOT NULL DEFAULT 90,
    grid_size INTEGER NOT NULL,                   -- 实际跑的 grid 点数(27 / 125 / ...)
    grid_method VARCHAR(32) NOT NULL DEFAULT 'product', -- 'product' | 'coordinate_descent'
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,  -- current_sharpe, candidate_sharpe, 等
    recommendation_id VARCHAR(128),              -- 产出的 rec(可为 null,见 R1-16)
    rejected_by_clamp BOOLEAN NOT NULL DEFAULT FALSE,
    clamp_violation_direction VARCHAR(16),       -- 'above_upper' / 'below_lower' / null
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    error_message TEXT                           -- 失败时填
);

CREATE INDEX IF NOT EXISTS ix_profile_research_profile_started
    ON governance.profile_research_runs(profile_id, started_at DESC);

-- profile_type_review streak(R1-08:支持原子 CAS)
CREATE TABLE IF NOT EXISTS governance.profile_type_review_streak (
    profile_id VARCHAR(64) PRIMARY KEY,
    clamp_violation_direction VARCHAR(16) NOT NULL,
    streak_count INTEGER NOT NULL DEFAULT 0,
    last_run_id VARCHAR(128) NOT NULL,           -- 用于 CAS 防重入
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    review_recommendation_id VARCHAR(128)        -- 已生成 review rec 填
);

COMMIT;
```

### 1.2 Research Job(v2 修正 R1-04/R1-05/R1-08/R1-16)

新模块 `aats/data_platform/research/profile_research_job.py`:

```python
def run_profile_research(
    project_root: Path,
    *,
    profile_id: str,
    oos_window_days: int = 90,
    grid_method: str = "product",  # 'product' | 'coordinate_descent'
    dry_run: bool = False,
) -> dict[str, Any]:
    """对指定 profile 跑 grid search,必产 ≥1 条 rec(R1-16)。

    主流程:
      1. 读 clamp ← get_profile_clamps(profile_id)(R1-04 single source)
      2. 生成 grid(R1-05):
         - product mode: 3×3×3 = 27 points(快速跑通)
         - coordinate descent: 分三轮,每轮只动一维 5 点 = 15 points
      3. 读近 oos_window_days 的 replay bars,对每 grid point 算 Sharpe / MaxDD / Activity
      4. 选 OOS Sharpe 最高 candidate
      5. Profile Gate 预校验(§1.4)
      6. 分支:
         (a) candidate 在 clamp 内 + Gate pass      → produce `profile_upgrade`
         (b) candidate == current(差异 ≤ 5%)        → produce `profile_keep_active`(R1-16)
         (c) candidate 超 clamp                      → rejected_by_clamp=true,streak++
             - streak ≥ 3 且无 review rec          → produce `profile_type_review`
    """
```

**Clamp single source(R1-04):**

```python
# aats/data_platform/research/profile_clamps.py  (新文件)
from typing import TypedDict

class ClampRange(TypedDict):
    lo: float
    hi: float

PROFILE_CLAMPS: dict[str, dict[str, ClampRange]] = {
    "trend_normal": {
        "strategy_entry_min_signal_edge_bps": {"lo": 1.5, "hi": 13.0},
        "strategy_entry_alpha_min": {"lo": 0.10, "hi": 0.30},
        "strategy_min_net_edge_bps": {"lo": 2.0, "hi": 10.0},
    },
    "trend_fast": {...},
    "trend_slow": {...},
    "balanced": {...},
    "defensive": {...},
}

def get_profile_clamps(profile_id: str) -> dict[str, ClampRange]:
    """Clamp 的唯一入口。seed + research 都调这个,保 single source of truth。"""
    if profile_id not in PROFILE_CLAMPS:
        raise KeyError(f"unknown profile: {profile_id}")
    return PROFILE_CLAMPS[profile_id]
```

且 `aats/services/parameter_store/strategy_profile_seed.py` 的 `_clamp_float` 改造:
```python
from aats.data_platform.research.profile_clamps import get_profile_clamps
clamps = get_profile_clamps(profile_id)
value = _clamp_float(raw_value, **clamps["strategy_entry_min_signal_edge_bps"])
```

**Streak 原子 CAS(R1-08):**

```python
# aats/data_platform/governance/profile_streak_db.py  (新文件)
def increment_streak_atomic(
    session: Session,
    *,
    profile_id: str,
    direction: str,
    new_run_id: str,
) -> int:
    """Streak 自增的原子 CAS:只在 last_run_id 不是 new_run_id 时才 +1。

    防范:同一个 research run 重复提交(dedup by run_id)。
    """
    # 用 INSERT ... ON CONFLICT DO UPDATE + WHERE 实现 CAS
    stmt = text("""
        INSERT INTO governance.profile_type_review_streak
            (profile_id, clamp_violation_direction, streak_count,
             last_run_id, last_updated)
        VALUES
            (:pid, :dir, 1, :run_id, NOW())
        ON CONFLICT (profile_id) DO UPDATE SET
            streak_count = CASE
                WHEN governance.profile_type_review_streak.clamp_violation_direction = EXCLUDED.clamp_violation_direction
                 AND governance.profile_type_review_streak.last_run_id != EXCLUDED.last_run_id
                THEN governance.profile_type_review_streak.streak_count + 1
                WHEN governance.profile_type_review_streak.clamp_violation_direction != EXCLUDED.clamp_violation_direction
                THEN 1  -- 方向变了,重置
                ELSE governance.profile_type_review_streak.streak_count  -- run_id 相同,no-op
            END,
            clamp_violation_direction = EXCLUDED.clamp_violation_direction,
            last_run_id = EXCLUDED.last_run_id,
            last_updated = NOW()
        RETURNING streak_count
    """)
    result = session.execute(stmt, {"pid": profile_id, "dir": direction, "run_id": new_run_id})
    return result.scalar_one()
```

**Grid(R1-05) 默认降档:**

| 参数 | 范围(clamp 驱动) | 步数(v2) | 说明 |
|------|------|------|------|
| strategy_entry_min_signal_edge_bps | clamp.lo..clamp.hi linspace | **3** | 从 8 降到 3 |
| strategy_entry_alpha_min | [0.10, 0.30] | **3** | 从 5 降到 3 |
| strategy_min_net_edge_bps | clamp.lo..clamp.hi linspace | **3** | 从 5 降到 3 |

总 3×3×3 = **27 points**。运行稳定后可通过 env `AATS_RDP_PROFILE_GRID_SIZE` 升到 5×5×5=125 或切 coordinate descent。

### 1.3 Recommendation 扩展(v2 修正 R1-02)

- `scope='profile'`、`scope_ref=profile_id`
- `family=NULL`、`timeframe=NULL`(CHECK 允许)
- `target_parameter_set_id` 指向一个 `parameter_sets` 记录(scope='profile')
  - 其 `values` JSONB 存 `threshold_patches`:
    ```json
    {
      "strategy_entry_min_signal_edge_bps": {"from": 13.0, "to": 10.2},
      "strategy_entry_alpha_min": {"from": 0.18, "to": 0.15},
      "strategy_min_net_edge_bps": {"from": 4.5, "to": 3.8}
    }
    ```
- `evidence_bundle_ref` 指向 `profile_research_runs.run_id`

### 1.4 Gate 规则(不变)

`aats/data_platform/gates/profile_gate.py` 维持 v1 的三指标逻辑(Sharpe ≥ 0.95, MaxDD ≤ 1.05, Activity ≥ 0.50),详略同 v1 §1.4。

### 1.5 API 端点 + apply_token(v2 修正 R1-07)

| 方法 | 路径 | apply_token action | 说明 |
|------|------|---------|------|
| GET | `/rdp/profile-recommendations` | — | 列表 |
| GET | `/rdp/profile-recommendations/{id}` | — | 详情 |
| POST | `/rdp/profile-recommendations/{id}/approve` | `approve` | 审批(非一键) |
| POST | `/rdp/profile-recommendations/{id}/reject` | `reject` | 拒绝 |
| POST | `/rdp/profile-recommendations/{id}/gate` | — | 运行 profile_gate,不改状态 |
| POST | `/rdp/profile-recommendations/{id}/release` | `release` | gate pass 后建 release |
| POST | `/rdp/profile-recommendations/{id}/apply` | **`apply`** | 写 DB(跨库 saga,§1.6) |
| POST | `/rdp/profile-recommendations/{id}/rollback` | **`rollback`** | 回滚到上一 profile 版本 |
| GET | `/rdp/profile-type-reviews` | — | 列 streak ≥ 3 项 |
| POST | `/rdp/profile-type-reviews/{id}/resolve` | `review_resolve` | operator 处置(见 1.8) |

**关键(R1-07):** `apply_token.action` 不新增值;服务端在 `/apply` handler 中按 `recommendation.scope` 决定:
- `scope in ('combo', 'cost_model')` — 单签即可
- `scope in ('profile', 'sleeve', 'risk')` — 需要 `approver_a` 和 `applier_b` 两个不同 actor(operator token 里带 actor_id,服务端 reject 同 actor)

### 1.6 跨库 Apply Saga(v2 新增,解决核心 Blocker R1-20)

**目标:** `apply` 一次必须同时写 research DB 和 live DB,让实盘真的吃到新 profile。

**四步 saga**(幂等键 = `parameter_apply_history.operation_id`):

```python
# aats/data_platform/governance/profile_apply_saga.py (新文件)

def apply_profile_saga(
    *,
    research_session: Session,      # RDP_DATABASE_URL
    live_session: Session,          # AATS_LIVE_DB_URL (R/W,见 §1.7)
    recommendation_id: str,
    actor_id: str,
    apply_token: str,
) -> ApplySagaResult:
    """
    四步(前两步在 research DB,后两步在 live DB):

    Step 1 [research] — UPSERT active_parameter_sets(scope='profile', scope_ref=profile_id)
                        到 target_parameter_set 引用的 values
    Step 2 [research] — INSERT parameter_apply_history(scope='profile', scope_ref=profile_id,
                        operation_id=..., from_ps, to_ps, actor_id)
    Step 3 [live]     — UPSERT strategy_profile_activation 的 payload:
                        把 research 里 threshold_patches 合进对应 profile 的 payload,
                        actor 改为 'rdp_apply'
    Step 4 [live]     — INSERT strategy_profile_activation_history 审计事件

    幂等性:
    - operation_id 由 (recommendation_id + target_parameter_set_id) 哈希得到
    - 每步 SQL 都走 "ON CONFLICT DO NOTHING" / "NOT EXISTS"
    - 重试安全:任一步骤失败,可重放,已完成的 step 不会重写

    失败补偿:
    - Step 1/2 失败      → 整体 abort,无副作用
    - Step 3 失败        → research 已写,live 未写:
                            1) 告警 "research/live drift"
                            2) 同 operation_id 再跑一次,Step 1/2 幂等跳过,只补 Step 3/4
                            3) 补偿重试失败超 5 次 → operator 介入
    - Step 4 失败        → live payload 已写但 history 未写:
                            1) 告警 "history missing"
                            2) operator 手动补 history
    """
```

**为什么不做 2PC:** Postgres 跨库 2PC 要 `PREPARE TRANSACTION` + 分布式协调器,重;本系统只有 research/live 两个库,saga + 幂等键 + 补偿就够,且可用 `operation_id` 查缺失步骤。

**补偿脚本:** 新增 `scripts/rdp_apply_saga_reconcile.py` 扫描 `parameter_apply_history` 找"已写 research 但 live 无对应 activation_history"的 drift,支持 `--dry-run` / `--operation-id <id>` 定点修复。

### 1.7 Cross-DB 连接(v2 新增,R1-09 + R1-10 预留)

**Env 新增:**

```bash
# .env.derivatives.live 新增
AATS_LIVE_DB_URL=postgresql+asyncpg://admin:***@aats-postgres:5432/aats_live_derivatives
# RDP daemon 独立读写连接(和 gateway 用的 live 连接分开,便于 audit)
AATS_LIVE_DB_URL_RDP=postgresql+psycopg://admin:***@aats-postgres:5432/aats_live_derivatives
```

RDP daemon 启动时多开一个 connection pool:

```python
# aats/data_platform/runtime/session.py (扩展)

def get_live_session(mode: str = "rw") -> Session:
    """RDP 进程访问 live DB 的统一入口。

    mode='rw'  — 用于 apply saga 的 Step 3/4
    mode='ro'  — 用于 cost calibration 读 execution_fills(Phase 2)
    """
```

连接池大小: 5(足够 weekly research + daily cost calibration);连接超时 10s。

### 1.8 Shadow 期(v2 修正 R1-06)

- Feature flag 存 `governance.system_config`:`key='profile_upgrade_auto_apply_enabled'`, `value=false/true`
- Shadow 期(上线后 4 周)apply API 先检查 flag:
  ```python
  flag = get_system_config("profile_upgrade_auto_apply_enabled", default=False)
  if not flag and recommendation.scope == 'profile':
      raise HTTPException(403, "profile apply disabled; shadow period")
  ```
- 4 周后 operator 手动 flip:
  ```
  POST /rdp/system-config/profile_upgrade_auto_apply_enabled
    body: {"value": true, "apply_token": "..."}
  ```
- Shadow 期内 Gate / approve / release 照跑,只 apply 禁用

### 1.9 回滚(v2 修正 R1-11)

- schema 回滚 → `batch_b_99_rollback.sql`(§1.1 已给)
- 运行时回滚 → `POST /rdp/profile-recommendations/{id}/rollback`:
  1. 查 `parameter_apply_history WHERE scope='profile' AND scope_ref=profile_id ORDER BY created_at DESC LIMIT 2`
  2. 取倒数第二条作为 rollback target
  3. 走同一个 `apply_profile_saga` 但 `to_parameter_set_id = 前一版`
  4. 审计记录 `rollback_of_operation_id` 填回前一 operation

### 1.10 UI(v2 修正 R1-13 Hero 整合)

**Hero 顶带**(`aats/api/rdp_control_summary.py`):

| 字段 | 含义(v2) | SQL 来源 |
|------|---------|---------|
| pending_review | 待审批(combo + profile 合计) | recommendations WHERE status='draft' |
| observation | 观察中 | release 已 apply 且在 observation 期 |
| blocked | 阻断 | Gate 未 pass 的 release |
| queue | 执行队列 | rdp_task_queue 未处理项 |
| **pending_type_review** (新) | **待人工审查 profile** | recommendations WHERE type='profile_type_review' AND status='draft' |

UI 点击 pending_review 列表时,combo 和 profile 分栏显示,用 scope 筛选器切换。

Dashboard 新区块(保留 v1 设计,补充 scope='profile' badge):

```
┌─ Profile-level Recommendations ─────────────────────┐
│ [profile_upgrade · scope=profile] trend_normal      │
│ ├ min_signal_edge_bps: 13.0 → 10.2                  │
│ ├ alpha_min: 0.18 → 0.15                            │
│ ├ Sharpe: 1.23 (+ 5%)    MaxDD: -8%   Activity ok   │
│ └ [Gate] [Approve] [Reject]                         │
└─────────────────────────────────────────────────────┘
```

### 1.11 测试

| 测试文件 | 覆盖 | 审查 ID |
|----------|------|---------|
| `test_batch_b_01_migration.py` | CONSTRAINT drop + partial unique index 生效 | R1-01 |
| `test_profile_scope_insert.py` | family=NULL + scope=profile 插入通过,scope=combo + family=NULL 被 CHECK 拒 | R1-02/03 |
| `test_profile_research_grid_sizes.py` | 27 grid / coordinate descent 都能跑完 | R1-05 |
| `test_profile_streak_atomic_cas.py` | 并发两个 run 提交,streak 只 +1 一次 | R1-08 |
| `test_profile_clamp_single_source.py` | seed 和 research 读的是同一个 `get_profile_clamps` | R1-04 |
| `test_profile_apply_saga_happy_path.py` | 四步全 OK | R1-20 |
| `test_profile_apply_saga_step3_retry.py` | Step 3 first try 失败,第二次补齐 | R1-20 |
| `test_profile_apply_saga_idempotent.py` | 同 operation_id 重放不重写 | R1-20 |
| `test_system_config_get_set.py` | feature flag read/write + token check | R1-06 |
| `test_apply_token_action_reuse.py` | profile apply 走 action=apply,不加新 action | R1-07 |
| `test_rec_type_backcompat.py` | 新 VALID_REC_TYPES 值不破既有断言 | R1-12 |
| `test_parameter_apply_history_scope.py` | scope 列存在且 index 可用 | R1-11 |
| `test_rdp_profile_routes.py` | API 200/403/409 全路径 | Phase 1 |

---

## Phase 2:Cost Model 校准(v2)

### 2.1 数据模型(v2 不变)

Migration `batch_b_03_cost_calibration.sql` 同 v1 §2.1。

### 2.2 Research Job(v2 修正 R1-09/R1-15)

**跨库读:** RDP 进程用 `get_live_session(mode='ro')` 访问 live DB 的 `execution_fills`。

**decision_price 字段定义(R1-15):**

```python
def resolve_decision_price(order: ExecutionOrderModel) -> Decimal | None:
    """明确 decision_price 的定义:

    - 限价单(order.order_type='limit') → order.limit_price
    - 市价单(order.order_type='market') → order.raw_payload['decision_mid_price']
        (由 execution 服务在下单前写入 raw_payload,见 2.2.1 新增契约)
    - 两者都缺 → 返回 None,此笔 fill 从 slippage 样本排除
    """
```

**2.2.1 execution 侧契约(预置):** decision_mid_price 写入需要 execution 服务在下单时把决策时的 best mid 放进 raw_payload。如果当前未记录,Phase 2 上线前先打一个小 PR 往 execution 路径注入 `decision_mid_price`(一次性改动,非本 SOW 交付)。

**Slippage 公式(明确方向):**

```python
# 对每笔 fill i:
# side='buy'  → slip_bps[i] = (fill_price[i] - decision_price[i]) / decision_price[i] × 10000
# side='sell' → slip_bps[i] = (decision_price[i] - fill_price[i]) / decision_price[i] × 10000
# 买单付得越高于 decision → 正 slip;卖单卖得越低于 decision → 正 slip
# 加权:sum(|qty| * slip_bps) / sum(|qty|)
```

**Fee 公式:**

```python
effective_fee_bps = sum(fee_amount) / sum(fill_qty * fill_price) × 10000
```

**Drift 阈值 1.5 bps:** 超过则产出 `parameter_upgrade` rec,`scope='cost_model'`,`review_notes` 带 `source=cost_calibration` + `drift=X.X bps`。本 scope 仍写 combo-level `taker_fee_bps`/`slippage_bps`,所以 scope='cost_model' 仅做 source 标记,family/timeframe 都要填(R1-11 后 CHECK 允许)。

### 2.3 API / UI / Guardrail / 测试

同 v1 §2.3-2.6,补充 Feature flag `cost_calibration_auto_recommend_enabled`(初始 false 的 shadow 期 2 周)。

---

## Phase 3:Sleeve Budget Advice(v2 修正 R1-17,统一为 recommendation)

### 3.1 数据模型(v2 采用方案 A,删 advice 表)

**不再建 `sleeve_budget_advice` 独立表。** 所有 sleeve 建议都走既有 `recommendations` 表:

- `scope='sleeve'`, `scope_ref=sleeve_id`
- `recommendation_type` ∈ `{'sleeve_budget_adjust', 'sleeve_budget_keep'}`
- `target_parameter_set_id` → 一个 `parameter_sets`(scope='sleeve') 存建议的 `budget_pct`
- observation-only 的语义靠 **UI + API 约束** 实现:不提供 apply 按钮(sleeve rec 的 /apply endpoint 返回 403 "sleeve budget advice is observation-only")

Migration `batch_b_04_sleeve_advice.sql` 现在只建一个小 helper 视图:

```sql
CREATE OR REPLACE VIEW governance.vw_sleeve_advice_recent AS
SELECT
    r.recommendation_id,
    r.scope_ref AS sleeve_id,
    r.recommendation_type,
    r.evidence_bundle_ref,
    r.reason,
    r.status,
    r.created_at,
    ps.values AS proposed
FROM governance.recommendations r
LEFT JOIN governance.parameter_sets ps ON r.target_parameter_set_id = ps.parameter_set_id
WHERE r.scope = 'sleeve'
ORDER BY r.created_at DESC;
```

### 3.2 Research Job

不变(v1 §3.2),仍 weekly。跨库读 `strategy_sleeve_intents` 走 `get_live_session(mode='ro')`(R1-10 解决)。

### 3.3 API(v2 整合到 /rdp/recommendations)

| 方法 | 路径 | 行为 |
|------|------|------|
| GET | `/rdp/sleeve-advice/recent` | SELECT FROM vw_sleeve_advice_recent LIMIT N |
| POST | `/rdp/recommendations/{id}/mark-reviewed` | 对所有 scope 通用的"已审阅"标记(不改 status,加 note) |
| POST | `/rdp/recommendations/{id}/apply` | **scope='sleeve' 的 rec 返回 403** |

### 3.4 UI / Guardrail / 测试

UI 保持 v1 §3.4 的 observation-only 展示风格;Guardrail 加硬编码 in handler:

```python
if rec.scope == 'sleeve':
    raise HTTPException(403, "sleeve budget recommendations are observation-only")
```

测试加 `test_sleeve_apply_returns_403.py`。

---

## Phase 4:Risk Guardrail(占位,同 v1 §4)

不改。

---

## 5. 跨 Phase 依赖图(v2 新增 saga 依赖)

```
Phase 0 (已完成):release_cycle in-process 修复
    │
    ▼
Phase 1 基建(batch_b_01 + system_config + live cross-DB pool)
    │   严格顺序:先跑 migration,再部署 code
    │
    ├── Phase 1a research(batch_b_02 + profile_research_job)
    │       产 recommendation,Shadow flag 关
    │
    ├── Phase 1b apply saga(profile_apply_saga.py + /apply endpoint)
    │       4 周 Shadow 观察 → flip flag → 进入 live apply
    │
    ▼
Phase 2(batch_b_03 + cost_calibration_job + decision_price 契约 PR)
    │   独立上线,但依赖 Phase 1 的 system_config + cross-DB 基建
    │
    ▼
Phase 3(batch_b_04 vw_sleeve_advice_recent + sleeve_advice_job)
    │   observation-only,独立
    │
    ▼
Phase 4(独立 SOW;等 Phase 1-3 运行 8 周)
```

---

## 6. Observability(v2 修正 R1-18)

### 6.1 Metrics / Grafana

保留 v1 §6 的 4 条 dashboard 面板,新增:

### 6.2 Alerts(v2 增强)

**既有:**
- profile_upgrade Gate 失败率 > 60%(7 天)→ seed clamp 可能过严
- cost drift > 3 bps(连续 2 次)→ cost model 需紧急校准
- release_cycle task failure rate > 10%(24h)

**新增(R1-18):**
- **workflow failure rate > 20%(1h rolling)** — 任何 workflow(不只 release_cycle)连挂 ≥ 2 次 → 告警
- **daemon heartbeat 停止 > 10 分钟** — RDP daemon 无 heartbeat → 告警(防 Phase 0 类进程死循环,实际上是进程卡死)
- **rdp_task_queue pending > 5 超过 10 分钟** — 队列堆积 → 告警
- **apply saga drift > 0** — research 已写但 live 未写的 operation_id 数量 > 0 → P1 告警(R1-20 的自检)

### 6.3 Heartbeat 实现

RDP daemon 每 60s 写一次 `governance.system_config.key='rdp_daemon_heartbeat'`,Grafana 监控这个 key 的 `updated_at`。

---

## 7. Deployment 顺序(v2 修正 R1-14,沿用 batch_a 模式)

**Migration runner:** `aats/data_platform/migrations/_batch_b.py`(新建),仿 `_batch_a.py`:

```python
# _batch_b.py
BATCH_B_SQL_FILES = [
    "batch_b_01_scope_columns.sql",
    "batch_b_02_profile_research.sql",
    "batch_b_03_cost_calibration.sql",
    "batch_b_04_sleeve_advice.sql",
]

def run_batch_b_migrations(engine, *, dry_run: bool = False) -> BatchBReport:
    """类似 run_batch_a_migrations,串行执行,每步可回滚。"""
```

**Deployment SOP(逐步):**

1. **Pre-check** — 连测试数据库先跑 `batch_b_99_rollback.sql`(空库下应该无-op),再正向跑全套 → 再 rollback → 再正向;保证 rollback 脚本可用(R1-19)
2. **合并 batch_b_01** 到代码库 — 跑 migration,部署 Phase 1 代码(shadow flag=false)
3. **观察 4 周** Shadow(Gate / approve / release 正常,apply 拒)
4. **Operator 开 flag** — POST /rdp/system-config/profile_upgrade_auto_apply_enabled value=true
5. **合并 batch_b_02 + cost calibration 代码** — Phase 2 启动,shadow 2 周
6. **合并 batch_b_03 + sleeve advice** — Phase 3 启动(observation-only,无 shadow)
7. **Phase 4** 独立 SOW

---

## 8. Review Checkpoints(v2)

Phase 1 上线前必检:
- [ ] `batch_b_01_scope_columns.sql` CONSTRAINT drop 在复制库跑通
- [ ] `batch_b_99_rollback.sql` 可 roundtrip(正→反→正→反)
- [ ] `VALID_REC_TYPES` 补充后全局 grep 所有断言都改完
- [ ] `get_profile_clamps` 是 seed 和 research 的唯一 clamp 源
- [ ] Shadow flag 默认 false + API apply 端点尊重
- [ ] `AATS_LIVE_DB_URL_RDP` 已配置,RDP daemon 能同时连两库
- [ ] `profile_apply_saga` 的 happy path + Step 3 retry + 幂等重放都有单元测试
- [ ] Live DB `strategy_profile_activation.payload` 合并逻辑不覆盖非 RDP 管辖字段(只改 threshold_patches 键)
- [ ] Hero 顶带新增 `pending_type_review` 字段
- [ ] Daemon 4 条告警都在 Grafana rules 文件里

Phase 2 上线前必检:
- [ ] execution 服务已写入 `raw_payload.decision_mid_price`(先决契约 PR 合入)
- [ ] cost_calibration 读 `get_live_session(mode='ro')` 跑 7 天回填验证
- [ ] drift 计算方向正确(买单正 slip 情况,卖单正 slip 情况各一单测)

Phase 3 上线前必检:
- [ ] sleeve scope 的 /apply endpoint 返回 403
- [ ] vw_sleeve_advice_recent 视图对既有 SELECT 无影响

---

## 9. 未决 Warning(v2 暂留)

- R1-05 warn:grid_method='coordinate_descent' 逻辑 v2 给了 API,但具体收敛判据(何时停)需要实测后微调,Phase 1 shadow 期打 metric
- R1-16 已采纳:必产 rec 语义明确(profile_keep_active 新增到 VALID_REC_TYPES)

## 10. Blocker / Warning 解决状态

| ID | 状态 |
|----|------|
| R1-01 | ✅ 用 ALTER ... DROP CONSTRAINT + partial unique index 替换 |
| R1-02 | ✅ family / timeframe NULLABLE + scope-aware CHECK |
| R1-03 | ✅ 同上 |
| R1-04 | ✅ `get_profile_clamps` single source |
| R1-05 | ✅ 27 grid + coordinate descent 备选 |
| R1-06 | ✅ `governance.system_config` KV 表 |
| R1-07 | ✅ 复用 `action=apply`,服务端按 scope 决定双签 |
| R1-08 | ✅ 原子 CAS UPDATE 表达式 |
| R1-09 | ✅ `AATS_LIVE_DB_URL_RDP` 独立连接池 |
| R1-10 | ✅ 同 R1-09 |
| R1-11 | ✅ `parameter_apply_history` 加 `scope` + `scope_ref` |
| R1-12 | ✅ 清单化补 + 测试拆 |
| R1-13 | ✅ Hero 加 `pending_type_review` |
| R1-14 | ✅ `_batch_b.py` + SQL 文件模式沿用 batch_a |
| R1-15 | ✅ decision_price 明确 fallback 顺序 + Phase 2 先决 PR |
| R1-16 | ✅ 必产 keep_active(新 rec_type) |
| R1-17 | ✅ 方案 A,统一 recommendations 表 |
| R1-18 | ✅ 4 条新告警 |
| R1-19 | ✅ batch_b_99_rollback.sql + SOP |
| R1-20 | ✅ 跨库 saga + 幂等 + 补偿脚本 |

**10 blocker + 9 warning 全部 resolved in v2。**
