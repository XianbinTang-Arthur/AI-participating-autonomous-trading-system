# RDP 能力扩展 · 详细设计 (Phase 1-4)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 起草时间: 2026-04-18
> 本文件是 [rdp_scope_expansion_sow.md](./rdp_scope_expansion_sow.md) 的实施细化。
> 每个 Phase 包含:数据模型、research job、API、UI、guardrail、测试、回滚。

---

## 0. 全局约定

### 0.1 所有 Phase 必须满足

- Schema 改动走新的 `batch_b_NN_*.sql` migrations,不动 batch_a
- 任何新 scope 的 write 路径都经 `apply_token` HMAC 校验(复用 `aats/api/rdp_apply_token.py`)
- 所有新表加 FK 到既有 `governance.recommendations(recommendation_id)` 或 `parameter_releases(release_id)` 便于审计追溯
- 新 recommendation_type 加进 `VALID_REC_TYPES`(`_db_util.py:49-52`)
- 所有 API 走 `aats/api/rdp_routes.py` 既有的 Session 认证 + 速率限制

### 0.2 Scope 字段统一

引入全局 `scope` 枚举,覆盖未来所有 Phase:

```python
VALID_SCOPES = frozenset({
    "combo",      # 现有:family × timeframe 组合参数(Phase 0 before)
    "profile",    # Phase 1:strategy profile 级阈值
    "cost_model", # Phase 2:cost model 校准(仍写 combo 参数,但来源标识)
    "sleeve",     # Phase 3:sleeve budget 建议
    "risk",       # Phase 4:风控边界动态化
})
```

所有 `recommendations` 和 `parameter_sets` 表默认 `scope='combo'`,新 scope 走新字段。

---

## Phase 1:Profile-level 参数纳管

### 1.1 数据模型变更

#### Migration `batch_b_01_scope_columns.sql`

```sql
-- 1. recommendations 加 scope + scope_ref
ALTER TABLE governance.recommendations
    ADD COLUMN scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN scope_ref VARCHAR(128);  -- profile_id / sleeve_id / null(combo 时)

CREATE INDEX ix_rec_scope_ref ON governance.recommendations(scope, scope_ref, status);

ALTER TABLE governance.recommendations
    ADD CONSTRAINT chk_rec_scope CHECK (
        scope IN ('combo', 'profile', 'cost_model', 'sleeve', 'risk')
    );

-- 2. parameter_sets 加 scope
ALTER TABLE governance.parameter_sets
    ADD COLUMN scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN scope_ref VARCHAR(128);

-- 3. active_parameter_sets 同步
ALTER TABLE governance.active_parameter_sets
    ADD COLUMN scope VARCHAR(32) NOT NULL DEFAULT 'combo',
    ADD COLUMN scope_ref VARCHAR(128);

-- combo 场景下 (family, timeframe) 唯一仍然成立;scope 加入后,profile scope 的
-- 唯一约束是 (scope='profile', scope_ref=profile_id)——换 index 不换 PK。
DROP INDEX IF EXISTS governance.uq_active_combo;
CREATE UNIQUE INDEX uq_active_scope ON governance.active_parameter_sets(scope, scope_ref, family, timeframe)
    WHERE scope = 'combo';
CREATE UNIQUE INDEX uq_active_profile ON governance.active_parameter_sets(scope_ref)
    WHERE scope = 'profile';
```

#### Migration `batch_b_02_profile_research.sql`

```sql
-- Profile-level research 运行记录
CREATE TABLE IF NOT EXISTS governance.profile_research_runs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(128) NOT NULL UNIQUE,
    profile_id VARCHAR(64) NOT NULL,
    oos_window_days INTEGER NOT NULL DEFAULT 90,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,  -- current_sharpe, candidate_sharpe, etc.
    recommendation_id VARCHAR(128),              -- 产出的 profile_upgrade rec(可为 null)
    rejected_by_clamp BOOLEAN NOT NULL DEFAULT FALSE,
    clamp_violation_direction VARCHAR(16),       -- 'above_upper' / 'below_lower' / null
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX ix_profile_research_profile_started
    ON governance.profile_research_runs(profile_id, started_at DESC);

-- profile_type_review 追踪(连续 3 轮同方向 clamp 超界的汇总)
CREATE TABLE IF NOT EXISTS governance.profile_type_review_streak (
    profile_id VARCHAR(64) PRIMARY KEY,
    clamp_violation_direction VARCHAR(16) NOT NULL,
    streak_count INTEGER NOT NULL DEFAULT 0,
    last_run_id VARCHAR(128),
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    review_recommendation_id VARCHAR(128)  -- 已产生 review rec 时填
);
```

#### 新增 recommendation_type 值

```python
# _db_util.py
VALID_REC_TYPES = frozenset({
    "parameter_upgrade", "keep_active", "lower_priority",
    "pause", "require_review",
    # Phase 1 新增
    "profile_upgrade",         # profile-level 阈值调整
    "profile_type_review",     # 连续 3 轮超 clamp 触发的人工审查
    # Phase 3 新增
    "sleeve_budget_adjust",    # observation-only
})
```

### 1.2 Research Job

新 workflow config `configs/rdp_workflows/profile_research_cycle.json`:

```json
{
  "workflow": "profile_research_cycle",
  "description": "Profile-level 参数研究 + 三指标 Gate 预校验 + clamp 合规检查。",
  "schedule_hint": "weekly Sunday 10:00 UTC",
  "schedule": {
    "enabled": true,
    "frequency": "weekly",
    "weekday_utc": "SUN",
    "hour_utc": 10,
    "minute_utc": 0
  },
  "tasks": [
    {
      "name": "profile_research",
      "description": "对每个 active profile 跑 grid search,产出 profile_upgrade rec 或 profile_type_review",
      "command": "python -m aats.data_platform.research.profile_research_job --run",
      "timeout_seconds": 1800,
      "enabled": true,
      "allow_failure": false
    }
  ]
}
```

**新模块** `aats/data_platform/research/profile_research_job.py`:

```python
def run_profile_research(
    project_root: Path,
    *,
    profile_id: str,
    oos_window_days: int = 90,
    dry_run: bool = False,
) -> dict[str, Any]:
    """对指定 profile 的关键门槛跑 grid search,产出 recommendation 或 profile_type_review。

    主流程:
      1. 读当前 profile 的 payload + seed clamp 范围
      2. 生成 grid:entry_min_signal_edge_bps ∈ linspace(clamp_lo, clamp_hi, 8)
         + alpha_min ∈ linspace(0.10, 0.30, 5) + net_edge_bps ∈ linspace(3, 10, 5)
      3. 对每个 grid point,用近 90 天 replay bars 计算 Sharpe / MaxDD / 活跃度
      4. 选 OOS Sharpe 最高的 candidate
      5. 三指标 Gate 预校验(§1.4)
      6. Clamp 检查:candidate 在 clamp 内 → produce profile_upgrade rec
                     超出 clamp → rejected_by_clamp,更新 streak,≥3 → produce profile_type_review
    """
```

**Grid 粒度**(初始):

| 参数 | 范围 | 步数 | 说明 |
|------|------|------|------|
| strategy_entry_min_signal_edge_bps | clamp 区间内 linspace | 8 | 主门槛 |
| strategy_entry_alpha_min | [0.10, 0.30] | 5 | 信号强度 |
| strategy_min_net_edge_bps | [3.0, 10.0] | 5 | 净边际 |

总 8×5×5 = 200 grid points 每 profile。

### 1.3 Recommendation 扩展

- `scope='profile'`、`scope_ref=profile_id`、`family='_profile'`(占位,便于既有 combo 查询不误抓)
- `target_parameter_set_id` 指向一个 `parameter_sets` 记录(scope='profile')包含 `threshold_patches` JSON:

```json
{
  "strategy_entry_min_signal_edge_bps": {"from": 13.0, "to": 10.2},
  "strategy_entry_alpha_min": {"from": 0.18, "to": 0.15},
  "strategy_min_net_edge_bps": {"from": 4.5, "to": 3.8}
}
```

- `evidence_bundle_ref` 指向 `profile_research_runs.run_id`

### 1.4 Gate 规则(profile scope 专属)

新增 `aats/data_platform/gates/profile_gate.py`:

```python
class ProfileGateResult:
    sharpe_ratio: float          # candidate_sharpe / current_sharpe
    maxdd_ratio: float           # candidate_maxdd / current_maxdd
    activity_ratio: float        # candidate_trades_per_year / current_trades_per_year
    allow_apply: bool
    failures: list[str]
```

Gate 规则(§6.2 决定):

```python
def check_profile_gate(metrics: dict) -> ProfileGateResult:
    failures = []
    if metrics["sharpe_ratio"] < 0.95:
        failures.append(f"sharpe_ratio={metrics['sharpe_ratio']:.3f} < 0.95")
    if metrics["maxdd_ratio"] > 1.05:
        failures.append(f"maxdd_ratio={metrics['maxdd_ratio']:.3f} > 1.05")
    if metrics["activity_ratio"] < 0.50:
        failures.append(f"activity_ratio={metrics['activity_ratio']:.3f} < 0.50")
    return ProfileGateResult(
        sharpe_ratio=metrics["sharpe_ratio"],
        maxdd_ratio=metrics["maxdd_ratio"],
        activity_ratio=metrics["activity_ratio"],
        allow_apply=not failures,
        failures=failures,
    )
```

### 1.5 API 端点

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | `/rdp/profile-recommendations/{id}/approve` | 审批(不允许 approve-and-release) |
| POST | `/rdp/profile-recommendations/{id}/reject` | 拒绝 |
| POST | `/rdp/profile-recommendations/{id}/gate` | 运行 profile gate |
| POST | `/rdp/profile-recommendations/{id}/release` | 生成 release(需 gate pass) |
| POST | `/rdp/profile-recommendations/{id}/apply` | apply 到 live(需 apply_token + 二次确认) |
| GET | `/rdp/profile-recommendations` | 列出 pending/approved |
| GET | `/rdp/profile-type-reviews` | 列出待人工审查项 |

### 1.6 UI 改动

Dashboard 增加新区块(在现有 Combo Recommendations 之下):

```
┌─ Profile-level Recommendations ────────────────┐
│ [profile_upgrade] trend_normal                  │
│ ├ min_signal_edge_bps: 13.0 → 10.2             │
│ ├ alpha_min: 0.18 → 0.15                       │
│ ├ Sharpe: 1.23 (+ 5%)    MaxDD: -8% (ok)       │
│ ├ Activity: 120/yr (vs 180/yr, -33%)           │
│ └ [Gate] [Approve] [Reject]   (no one-click)   │
└─────────────────────────────────────────────────┘

┌─ ⚠ Profile Type Review ────────────────────────┐
│ trend_normal: min_signal_edge_bps 近 3 轮建议    │
│   均低于 clamp 下限 13.0(10.2 / 9.8 / 11.1)   │
│ 人工判断:                                       │
│   (a) 切换到 balanced profile                  │
│   (b) 放宽 clamp                               │
│   (c) 重新 seed                                │
│ [Mark as reviewed] [Ignore for 7 days]          │
└─────────────────────────────────────────────────┘
```

### 1.7 Shadow 期

- 数据库上引入 `feature_flag` 表项 `profile_upgrade_auto_apply_enabled`(默认 false)
- Shadow 期内(上线后 4 周)apply API 返回 403 + "shadow period"
- Gate 照常跑,approve 照常允许,但 release/apply 禁用
- 4 周后 operator 评审 Shadow 产出的 recommendations,手动开 flag

### 1.8 回滚路径

- **schema 回滚**:`batch_b_99_rollback.sql` 删新表 + 恢复 uq_active_combo
- **运行时回滚**:`POST /rdp/profile-recommendations/{id}/rollback` 用 apply_token 把 profile 换回上一版本;走既有 `parameter_apply_history` 表审计

### 1.9 测试

- **单元**
  - `test_profile_research_job_grid_search.py` — grid search + metric 计算
  - `test_profile_gate_rules.py` — 三指标 Gate 逻辑
  - `test_profile_clamp_streak.py` — 3 轮 streak 触发 review
  - `test_profile_recommendation_scope.py` — scope='profile' 的 insert/query
- **集成**
  - `test_profile_research_full_cycle.py` — research → recommend → gate → approve → (shadow) apply
- **API**
  - `test_rdp_profile_routes.py` — 所有端点的 200/403/409 路径

---

## Phase 2:Cost Model 校准

### 2.1 数据模型变更

#### Migration `batch_b_03_cost_calibration.sql`

```sql
CREATE TABLE IF NOT EXISTS governance.cost_calibration_runs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(128) NOT NULL UNIQUE,
    symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) NOT NULL,
    observation_days INTEGER NOT NULL DEFAULT 7,
    fills_count INTEGER NOT NULL,
    effective_taker_fee_bps NUMERIC(8, 3) NOT NULL,
    effective_slippage_bps NUMERIC(8, 3) NOT NULL,
    current_taker_fee_bps NUMERIC(8, 3) NOT NULL,
    current_slippage_bps NUMERIC(8, 3) NOT NULL,
    drift_bps NUMERIC(8, 3) NOT NULL,             -- |effective - current|
    recommendation_id VARCHAR(128),               -- 触发时填
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_cost_calib_sym_tf ON governance.cost_calibration_runs(symbol, timeframe, created_at DESC);
```

### 2.2 Research Job

新 workflow `configs/rdp_workflows/cost_calibration_cycle.json`:

```json
{
  "workflow": "cost_calibration_cycle",
  "description": "用最近 7 天成交实盘数据反推 effective taker fee + slippage,drift 超 1.5 bps 触发 parameter_upgrade rec。",
  "schedule_hint": "daily 06:00 UTC",
  "schedule": {
    "enabled": true,
    "frequency": "daily",
    "hour_utc": 6,
    "minute_utc": 0
  },
  "tasks": [
    {
      "name": "cost_calibration",
      "command": "python -m aats.data_platform.research.cost_calibration_job --run --lookback-days 7",
      "timeout_seconds": 600
    }
  ]
}
```

**新模块** `aats/data_platform/research/cost_calibration_job.py`:

```python
def calibrate_cost_from_fills(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    lookback_days: int = 7,
) -> dict[str, float]:
    """
    fee 反推:sum(fee) / sum(notional) × 10000
    slippage 反推:sum((fill_price - decision_price) × sign) / sum(notional) × 10000
                   (按 side 取 sign:long fill>decision 是反向 slip,short 反之)
    """
```

Drift 阈值 1.5 bps,超出则产出 `parameter_upgrade` rec(scope='combo' 但 `review_notes` 标 `source=cost_calibration`)。

### 2.3 API

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/rdp/cost-calibration/recent` | 最近 N 次校准 |
| GET | `/rdp/cost-calibration/drift-trend` | drift 的时序趋势(用于 Grafana) |

### 2.4 UI

Dashboard 右侧运行态栏新增:

```
Cost Drift (7d)
  taker_fee:  12.0 bps (config) | 11.8 bps (actual)   ✓ within 1.5
  slippage:    0.6 bps (config) |  1.9 bps (actual)   ⚠ +1.3 bps
  Last calibration: 2026-04-18 06:00 UTC
```

### 2.5 Guardrail

- 本 Phase 产出的 rec 只改 cost 字段(`taker_fee_bps`, `slippage_bps`, `expected_slippage_buffer_bps`),不改其他 combo 字段
- review_notes 必须带 `source=cost_calibration` + `drift=X.X bps`
- 若观察期 fills < 50 条 → 跳过本次 calibration 不产 rec(样本太少)

### 2.6 测试

- `test_cost_calibration_math.py` — fee/slippage 计算正确性
- `test_cost_calibration_threshold.py` — drift < 1.5 不产 rec / ≥ 1.5 产 rec
- `test_cost_calibration_insufficient_samples.py` — < 50 fills skip

---

## Phase 3:Sleeve Budget Advice(observation-only)

### 3.1 数据模型变更

#### Migration `batch_b_04_sleeve_advice.sql`

```sql
CREATE TABLE IF NOT EXISTS governance.sleeve_budget_advice (
    id SERIAL PRIMARY KEY,
    advice_id VARCHAR(128) NOT NULL UNIQUE,
    sleeve_id VARCHAR(128) NOT NULL,
    current_budget_pct NUMERIC(5, 2) NOT NULL,
    suggested_budget_pct NUMERIC(5, 2) NOT NULL,
    rationale JSONB NOT NULL,       -- {"edge_bps": ..., "sharpe": ..., "maxdd": ...}
    recommendation_id VARCHAR(128), -- FK 到 recommendations(scope='sleeve')
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- observation-only: 没有 approved / applied 字段;operator 采纳后手动改 sleeve_budget_profiles
    operator_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
    operator_note TEXT
);

CREATE INDEX ix_sleeve_advice_sleeve ON governance.sleeve_budget_advice(sleeve_id, created_at DESC);
```

### 3.2 Research Job

新 workflow `configs/rdp_workflows/sleeve_advice_cycle.json`(weekly):

对每个 sleeve 读 `strategy_sleeve_intents` + `execution_fills` 联合查询近 90 天 realized edge / sharpe / maxdd,按简单启发式:

- sharpe ≥ median + 1σ → 建议 budget + 5%
- sharpe ≤ median − 1σ → 建议 budget − 5%
- 其他 → 建议 hold

### 3.3 API

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/rdp/sleeve-advice/recent` | 最近 7 天所有 advice |
| POST | `/rdp/sleeve-advice/{id}/mark-reviewed` | operator 标记"已看过",不改 budget |

### 3.4 UI

Dashboard 新区块(observation-only, 不与 recommendation 混):

```
┌─ Sleeve Budget Advice (observation) ─────────────┐
│ directional_sleeve: 30% → suggest 25% (-5%)     │
│   edge: 3.2 bps/day  sharpe: 0.8  maxdd: -12%   │
│   [Mark reviewed]                                │
│                                                  │
│ independent_sleeve: 50% → suggest HOLD          │
│   edge: 4.8 bps/day  sharpe: 1.4                │
└──────────────────────────────────────────────────┘
```

### 3.5 Guardrail

- **observation-only**:没有 approve/apply 按钮,operator 只能 mark-reviewed
- 任何对 sleeve_budget_profiles 的实际修改仍然走手动编辑 + commit 路径(不动)

### 3.6 测试

- `test_sleeve_advice_heuristic.py` — sharpe tier 判定
- `test_sleeve_advice_api.py` — mark-reviewed 幂等

---

## Phase 4:Risk Guardrail 动态化(独立 SOW 占位)

### 4.1 范围(暂定)

- `max_leverage`, `max_gross_exposure`, `per_symbol_position_cap_usdt`
- 根据 portfolio realized vol / recent MaxDD / open-drawdown 动态收紧

### 4.2 blast radius

风控参数过紧 → 系统停转;过松 → 爆仓。和 profile 同属"blast radius 顶级",比 profile 还危险(profile 至少还有成交,风控过紧直接 circuit-break 全部 flow)。

### 4.3 Rollout 原则(独立 SOW 细化)

1. **Phase 4a** Shadow-mode(recommendation 产出但永不 auto-apply,4 周)
2. **Phase 4b** 小变化建议(|Δ| ≤ 5%),operator 手动 apply
3. **Phase 4c** 大变化建议(|Δ| > 5%),双 operator 签名

### 4.4 本 SOW 不实现 Phase 4

- 只保留 `scope='risk'` 的字段预留,不写 research job
- 独立 SOW 在 Phase 1-3 稳定运行 8 周后启动

---

## 5. 跨 Phase 依赖图

```
Phase 0 (已完成):release_cycle in-process 修复
    │
    ▼
Phase 1 (Schema batch_b_01-02 + scope 枚举 + profile research/gate/API/UI)
    │   严格要求:Shadow 4 周 → 运营评审 → 放开 flag
    │
    ▼
Phase 2 (Schema batch_b_03 + cost calibration daily job)
    │   可与 Phase 1 Shadow 期并行开发,但 apply 等 Phase 1 flag 开后
    │
    ▼
Phase 3 (Schema batch_b_04 + sleeve advice weekly, observation-only)
    │   独立上线,不依赖 Phase 1 apply 开启
    │
    ▼
Phase 4 (独立 SOW;等 Phase 1-3 运行 8 周)
```

---

## 6. Observability

所有 Phase 共用:

- `parameter_apply_history` 新增 `scope` 字段(phase 1 迁移时同步加)
- Grafana 新 dashboard `rdp_scope_expansion.json`
  - profile_upgrade Gate pass/fail 比
  - profile_type_review streak 分布
  - cost drift 时序
  - sleeve advice 产出频度
- Alerts:
  - profile_upgrade Gate 失败率 > 60%(7 天)→ 告警(seed clamp 可能过严)
  - cost drift > 3 bps(连续 2 次)→ 告警(cost model 需紧急校准)
  - release_cycle task failure rate > 10%(24h)→ 告警(Phase 0 回归)

---

## 7. Deployment 顺序

1. 合并 Phase 1 schema migration → 运行 migration(无任何代码跑)
2. 合并 Phase 1 代码(research job 灰度开启,只产 recommendation,shadow flag 关)
3. 观察 4 周 Shadow
4. operator 手动开 `profile_upgrade_auto_apply_enabled=true`
5. Phase 2 schema + 代码(daily 校准立即生效,无需 shadow)
6. Phase 3 schema + 代码(observation-only,无需 shadow)
7. Phase 4 独立 SOW

---

## 8. Review Checkpoints

Phase 1 上线前必检:
- [ ] `batch_b_01_scope_columns.sql` 对既有 `active_parameter_sets` 数据不破坏(DEFAULT 'combo' + 部分 unique index)
- [ ] `VALID_REC_TYPES` 补充后,既有反序列化代码不抛(向前兼容)
- [ ] Profile research job 的 90-day replay data 在 `aats_research.gold.market_swap_replay_bars_*` 可用
- [ ] Shadow flag 默认 false,且 API apply 端点对此尊重
- [ ] `strategy_profile_seed.py` 的 clamp 范围与 Profile research job 使用的 clamp 来源一致(single source of truth)
